# Visual Regression Gate — product brief

## Problem

Small CSS and layout changes ship unnoticed and break pages in ways unit
tests never see. We want a CI gate that catches unintended visual change
by comparing each page against a known-good screenshot.

## What it does

The Visual Regression Gate takes a list of pages — full URLs or named
routes against a base URL — and a single **viewport size**. For each page
it opens a **headless Chromium**, waits for the page to settle, captures a
screenshot, and compares that screenshot to a stored **baseline image**
for that page, pixel by pixel.

## Settling before capture

Before capturing, the gate waits for the network to be idle (no in-flight
requests for 500 ms) and for all **web fonts to have loaded**, so text
does not render in a fallback face in one run and the real face in the
next.

## Pass and fail

A page **fails** when more than **0.1% of its pixels** differ from the
baseline. Anything at or below that is a pass.

Callers can mark regions to ignore by CSS selector — timestamps, ad
slots, carousels. The gate **masks those regions** (fills them with a flat
colour) in both images before comparing, so churn inside them does not
fail the page.

## First run

The first time a page is seen there is no baseline. The gate **stores that
run's screenshot as the baseline and passes** the page. Subsequent runs
compare against it.

## Updating baselines

Baselines are only ever replaced when the caller runs with
`--update-baselines`. The gate **never updates a baseline on its own**,
regardless of how small the difference is — an approved visual change is a
human decision.

## Rendering

All captures are taken at **1x device pixel ratio**. Retina/2x rendering
is out of scope for v1.

## Output

An **HTML report** that shows, for every page, the baseline image, the
current image, and a diff image highlighting changed pixels, side by side.

## Explicitly out of scope for v1

Accessibility auditing, cross-browser rendering (Firefox, WebKit),
interaction or flow testing, and performance metrics. This tool answers
one question: does this page still look like it did.
