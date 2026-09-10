# Changelog - yopu-pdf

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-09-06

### Changed
- **ULCS SDK In-Process Proxy Integration**: Migrated proxy resolution to in-process `ulcs.proxy` SDK calls instead of spawning external subprocesses.

## [1.1.0] - 2026-09-04

### Added
- **Multi-Lane Resilient Egress**:
  - Direct connection → VPS egress → residential proxy fallback chain using `curl_cffi`.
  - Realistic TLS fingerprinting to bypass anti-scraping challenges.

## [1.0.0] - 2026-09-04

### Added
- **Selectable Vector PDF Dumper for yopu.co (有谱么)**:
  - Lossless vector PDF generation (selectable text and chord symbols, not raster screenshots).
  - Support for full URLs (`https://yopu.co/view/...`) and bare sheet IDs.
  - Batch export capability for multiple sheets.
- **Page PNG Explosion (`--p` / `--images`)**:
  - Automatically bursts PDFs into sorted numbered page PNGs (default 200 DPI, configurable to 300 DPI).
  - Cleans up trailing orphaned pages on re-export.
- **Shell Convenience**:
  - `yp` shell alias and setup script (`setup.sh`).
