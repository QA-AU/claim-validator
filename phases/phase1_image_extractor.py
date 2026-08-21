"""Phase 1: Image extraction and captioning.

Images carry real content in the kinds of documents this pipeline reads —
architecture diagrams, screenshots, flowcharts, and tables rendered as pictures.
Before this module they were dropped silently: the loader extracted text only,
and `DocumentContent.images` was populated by nothing and read by nobody.

**Design: caption at ingest, not at extraction.** Each image is described by the
model once when the document is loaded, and the caption is spliced into the
document's text with a marker. Everything downstream then works unchanged —
chunking, TF-IDF retrieval, concept extraction, and provenance all operate on
text, so an image's content becomes retrievable like any paragraph.

The alternative — passing raw images into each extraction prompt — was rejected:
images are not retrievable, so there is no principled way to decide which images
belong with which concept probe, and every prompt would have to carry all of
them.

Cost is one model call per image, at ingest, once per document version.
"""

import base64
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Images below this are almost always icons, bullets, logos, or spacers —
# captioning them wastes a call and adds noise to the text.
MIN_IMAGE_BYTES = 8_000
MAX_IMAGES_PER_DOCUMENT = 50

SUPPORTED_MEDIA = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}

CAPTION_PROMPT = """Describe this image from a document so the description can \
stand in for the image in a text index.

State what it shows and transcribe any text, labels, values, or relationships \
it contains. If it is a diagram, describe the components and how they connect. \
If it is a table, transcribe the rows. If it is a screenshot, describe the \
interface and any visible values.

Be factual and specific. Do not speculate about intent. If the image carries no \
information (a logo, an icon, a decorative rule), reply with exactly: NO CONTENT"""


@dataclass
class ExtractedImage:
    """An image pulled out of a document, with where it came from."""

    data: bytes
    media_type: str
    source_document: str
    page: Optional[int] = None  # 1-indexed where the format has pages
    index: int = 0  # position within the document
    caption: str = ""

    @property
    def label(self) -> str:
        where = f"page {self.page}" if self.page is not None else f"image {self.index + 1}"
        return f"{self.source_document}, {where}"


def detect_media_type(data: bytes) -> Optional[str]:
    """Identify an image by magic bytes. Returns None if unrecognised."""
    for magic, media_type in SUPPORTED_MEDIA.items():
        if data.startswith(magic):
            return media_type
    return None


def extract_images_from_pdf(file_path: str, doc_name: str) -> List[ExtractedImage]:
    """Pull embedded images out of a PDF, page by page."""
    import pypdf

    images: List[ExtractedImage] = []
    try:
        with open(file_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page_no, page in enumerate(reader.pages, start=1):
                try:
                    page_images = page.images
                except Exception as e:  # a malformed page must not lose the document
                    logger.warning(f"Could not read images on page {page_no}: {e}")
                    continue

                for img in page_images:
                    if len(images) >= MAX_IMAGES_PER_DOCUMENT:
                        logger.warning(
                            f"Stopping at {MAX_IMAGES_PER_DOCUMENT} images for {doc_name}"
                        )
                        return images
                    data = img.data
                    if len(data) < MIN_IMAGE_BYTES:
                        continue
                    media_type = detect_media_type(data)
                    if not media_type:
                        continue
                    images.append(
                        ExtractedImage(
                            data=data,
                            media_type=media_type,
                            source_document=doc_name,
                            page=page_no,
                            index=len(images),
                        )
                    )
    except Exception as e:
        logger.error(f"Image extraction failed for {doc_name}: {e}")

    logger.info(f"Extracted {len(images)} images from {doc_name}")
    return images


def extract_images_from_docx(file_path: str, doc_name: str) -> List[ExtractedImage]:
    """Pull embedded images out of a Word document."""
    from docx import Document

    images: List[ExtractedImage] = []
    try:
        document = Document(file_path)
        for rel in document.part.rels.values():
            if "image" not in rel.reltype:
                continue
            if len(images) >= MAX_IMAGES_PER_DOCUMENT:
                logger.warning(f"Stopping at {MAX_IMAGES_PER_DOCUMENT} images for {doc_name}")
                break
            try:
                data = rel.target_part.blob
            except Exception as e:
                logger.warning(f"Could not read an embedded image in {doc_name}: {e}")
                continue
            if len(data) < MIN_IMAGE_BYTES:
                continue
            media_type = detect_media_type(data)
            if not media_type:
                continue
            images.append(
                ExtractedImage(
                    data=data,
                    media_type=media_type,
                    source_document=doc_name,
                    index=len(images),
                )
            )
    except Exception as e:
        logger.error(f"Image extraction failed for {doc_name}: {e}")

    logger.info(f"Extracted {len(images)} images from {doc_name}")
    return images


def caption_images(images: List[ExtractedImage], llm_client) -> List[ExtractedImage]:
    """Caption each image with the model, in place.

    A client without image support, or a per-image failure, degrades to an empty
    caption rather than losing the document — but it is logged, because a
    silently uncaptioned image is exactly the invisible gap this module exists
    to close.
    """
    if not images:
        return images

    describe = getattr(llm_client, "describe_image", None)
    if describe is None:
        logger.warning(
            f"LLM client {type(llm_client).__name__} has no describe_image(); "
            f"{len(images)} image(s) will contribute no content"
        )
        return images

    captioned = 0
    for image in images:
        try:
            caption = (describe(image.data, image.media_type, CAPTION_PROMPT) or "").strip()
        except Exception as e:
            logger.error(f"Captioning failed for {image.label}: {e}")
            continue

        if not caption or caption.upper().startswith("NO CONTENT"):
            logger.debug(f"{image.label}: no content")
            continue

        image.caption = caption
        captioned += 1

    logger.info(f"Captioned {captioned} of {len(images)} images")
    return images


def captions_as_text(images: List[ExtractedImage]) -> str:
    """Render captions as a text block for appending to the document.

    The marker keeps image-derived content identifiable once it is inside the
    chunk stream, so a claim traced back to a chunk can still be recognised as
    coming from a picture rather than prose.
    """
    parts = []
    for image in images:
        if not image.caption:
            continue
        parts.append(f"[IMAGE — {image.label}]\n{image.caption}")
    return "\n\n".join(parts)


def encode_for_api(data: bytes) -> str:
    """Base64-encode image bytes for an API image block."""
    return base64.standard_b64encode(data).decode("utf-8")
