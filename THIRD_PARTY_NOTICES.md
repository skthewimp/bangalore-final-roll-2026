# Sources and attribution

The official source is the Election Commission of India's Karnataka SIR Final
Roll 2026, published on 27 October 2026. `code/source.json` records the URL pattern,
the 28 selected constituencies and the expected polling-part counts. The count
transcription and the deletion/modification marker convention were informed by
[gouthamganeshm/Karnataka_Final_Roll_2026](https://github.com/gouthamganeshm/Karnataka_Final_Roll_2026).

The numerical `#` template used to distinguish modification marks is fetched
separately by `code/fetch_ocr_template.py` from upstream revision
`a44c7e524d2fc3a9bd465ccf68f7a3deb2455456`. Its SHA-256 is checked before use.
It is not bundled in this Git repository. GitHub's license endpoint did not
identify a license for the upstream project when this repository was prepared.
No license for that upstream asset is asserted here.

The MIT license covers the original code and documentation in this repository.
It does not relicense the official electoral rolls or external dependencies.
Python dependencies retain their own licenses. Tesseract is an external runtime
dependency. The public release contains derived booth counts only.
