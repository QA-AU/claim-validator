# Visual Regression Gate Specification

## Purpose

Define the behaviour of the Visual Regression Gate: capture a screenshot
of each supplied page, compare it to a stored baseline, and fail the run
when a page has changed more than the allowed amount.

## Requirements

### Requirement: Page capture
The gate SHALL open each supplied page in headless Chromium at the
caller-specified viewport size and capture a full-page screenshot.

#### Scenario: A page is captured
- **WHEN** a page URL and a viewport size are supplied
- **THEN** open the page in headless Chromium at that viewport
- **AND** capture a screenshot once the page has settled

### Requirement: Readiness wait before capture
The gate SHALL wait for the network to be idle and for all web fonts to
have finished loading before it captures a screenshot.

#### Scenario: A page is still loading resources
- **WHEN** the page has in-flight network requests or unloaded web fonts
- **THEN** wait until the network has been idle for 500 ms
- **AND** wait until `document.fonts.ready` has resolved
- **AND** only then capture the screenshot

### Requirement: Baseline comparison
The gate SHALL compare each captured screenshot to the stored baseline
image for that page, pixel by pixel.

#### Scenario: A baseline exists for the page
- **WHEN** a page has a stored baseline
- **THEN** compare the new screenshot to it pixel by pixel
- **AND** compute the percentage of pixels that differ

### Requirement: Difference threshold
The gate SHALL fail a page when more than 1% of its pixels differ from the
baseline, and pass it otherwise.

#### Scenario: A page has changed slightly
- **WHEN** 0.5% of a page's pixels differ from its baseline
- **THEN** pass the page

#### Scenario: A page has changed substantially
- **WHEN** 3% of a page's pixels differ from its baseline
- **THEN** fail the page

### Requirement: Masked regions
The gate SHALL exclude caller-specified regions, identified by CSS
selector, from the comparison by masking them in both images first.

#### Scenario: A page declares masked regions
- **WHEN** the caller supplies CSS selectors for regions to ignore
- **THEN** fill those regions with a flat colour in both the baseline and the new screenshot
- **AND** compare the masked images

### Requirement: First run for a page
The gate SHALL store the captured screenshot as the baseline and pass the
page when no baseline yet exists for it.

#### Scenario: A page is seen for the first time
- **WHEN** a page has no stored baseline
- **THEN** save the current screenshot as that page's baseline
- **AND** report the page as passed

### Requirement: Baseline update policy
The gate SHALL update a page's baseline automatically when the difference
is below 2%, and SHALL otherwise require the caller to pass
`--update-baselines`.

#### Scenario: A small change is detected
- **WHEN** a page differs from its baseline by less than 2%
- **THEN** replace the stored baseline with the new screenshot

#### Scenario: A large change is detected
- **WHEN** a page differs from its baseline by 2% or more
- **THEN** leave the baseline unchanged unless `--update-baselines` was passed

### Requirement: Device pixel ratio
The gate SHALL capture every page at 2x device pixel ratio for retina
fidelity.

#### Scenario: A page is captured
- **WHEN** a screenshot is taken
- **THEN** render the page at a device pixel ratio of 2

### Requirement: Report output
The gate SHALL produce an HTML report showing the baseline, current, and
diff image for every page, side by side.

#### Scenario: A run completes
- **WHEN** every page has been compared
- **THEN** write an HTML report
- **AND** include, per page, the baseline image, the current image, and a diff image

### Requirement: Accessibility check
The gate SHALL run an axe-core accessibility scan on every page and fail
the page when a serious or critical violation is found.

#### Scenario: A page has an accessibility violation
- **WHEN** axe-core reports a violation of impact `serious` or `critical`
- **THEN** fail the page
- **AND** list the violation in the report

### Requirement: Cross-browser capture
The gate SHALL additionally capture each page in headless Firefox and
headless WebKit and compare each against a per-browser baseline.

#### Scenario: A page is checked across browsers
- **WHEN** a page is submitted for comparison
- **THEN** capture it in Chromium, Firefox, and WebKit
- **AND** compare each capture against that browser's own baseline
