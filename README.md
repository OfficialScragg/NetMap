# NetMap
An Nmap scan result viewer. It helps pentesters work their way through large networks.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the app
python app.py
```

Then open `http://127.0.0.1:5000`.

## Importing a scan

- Put your nmap output in normal format (the `-oN` file).
- Open the Import page, paste the path, click Import.

This repo includes an example file: `full-scan-deep`.
