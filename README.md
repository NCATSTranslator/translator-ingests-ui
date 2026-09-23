# KGX Storage Web Server

A website for browsing and downloading KGX (Knowledge Graph Exchange) files from S3. No AWS login needed.

Live site: https://kgx-storage.ci.transltr.io

Repo: https://github.com/NCATSTranslator/translator-ingests-ui

## Summary

KGX Storage is a small web app. It lets anyone browse and download KGX files from the S3 bucket called `kgx-translator-ingests`. The data is built by a different repo (translator-ingests). This repo is only the website.

What the site does:
- Browse folders and files like a file manager. URLs look like `/releases/alliance/latest/`.
- Download files. The server creates temporary download links that last 1 hour.
- View JSON files in the browser with formatting.
- A docs page with copy-paste commands for curl, wget, and AWS CLI.

How it works: Your browser talks to Nginx over HTTPS. Nginx forwards to the Flask app. The Flask app talks to S3 with boto3. Folder sizes and file counts are precomputed into a file called `metrics.json` so pages load fast. You can run a cron job to refresh that file when new data is added.

Important paths:
- App lives at `/home/ubuntu/kgx-storage-webserver/`
- Systemd service name is `kgx-storage-webserver`. Its config file is in the repo and gets copied to `/etc/systemd/system/`.
- Logs: `/var/log/kgx-storage/` (access.log and error.log)
- Nginx config: copy `nginx-config` from the repo to `/etc/nginx/sites-available/kgx-storage`
- The metrics cache is `metrics.json` in the app folder. The script `compute_metrics.py` creates it. The script `update_metrics.sh` can run in cron to refresh it and tell Gunicorn to reload.

Setup in short: Clone the repo. Make a Python venv and install from requirements.txt. Run `sudo ./setup-webserver-service.sh`. Set up Nginx using the repo’s nginx-config. Run certbot for kgx-storage.ci.transltr.io. Run `compute_metrics.py` once so the site has folder stats. Optionally add a cron job for `update_metrics.sh`. The EC2 instance needs an IAM role that can read from S3, and the domain must point to the instance’s IP.

## Overview

This server gives people HTTP access to KGX files from the NCATS Biomedical Data Translator project. Anyone can browse and download the knowledge graph data stored in Amazon S3.

The pipeline that creates the data is in the translator-ingests repo. This repo is just the web interface. That way the data pipeline and the website can be updated separately.

Data pipeline code: https://github.com/NCATSTranslator/translator-ingests/tree/main/src/translator_ingest/util/storage

## Features

- **Browse the bucket.** You see folders and files in a list. You click to go deeper. URLs use paths like `/releases/alliance/latest/`. Old-style URLs with `?path=...` still work. They redirect to the new path-style URLs.
- **Download files.** The server creates temporary S3 links so you can download without AWS credentials. Links expire after 1 hour.
- **View JSON in the browser.** Open a JSON file and it shows formatted with syntax highlighting. You can download it from there too.
- **Docs page.** Lists commands for downloading with curl, wget, and AWS CLI. Includes common paths and how to extract .tar.zst archives.
- **HTTPS.** SSL is handled by Let's Encrypt. Certificates renew automatically.
- **No login.** The site is read-only and public. Anyone can use it.

## Architecture

Traffic flow:

1. User hits the site over HTTPS (port 443).
2. Nginx receives it, does SSL, and forwards to the Flask app on localhost port 5000.
3. The Flask app (run by Gunicorn) handles the request and calls S3 with boto3 when it needs to list or get files.
4. S3 holds the actual KGX files in the bucket `kgx-translator-ingests`.

Nginx: Handles HTTPS and passes requests to Flask. Good at connections and SSL.

Flask and Gunicorn: Flask has the routes and S3 logic. Gunicorn runs multiple workers so the app can handle several requests at once. Fixed routes are `/docs/` and `/public/`. A catch-all route handles everything else: paths like `/releases/alliance/latest/` list a folder; paths like `/releases/alliance/latest/graph-metadata.json` return the file (JSON as body or download for other types). Adding `?view` to a JSON file URL shows the HTML viewer. Legacy `/view/` and `/download/` URLs are no longer routed and return 404.

S3: The bucket `kgx-translator-ingests` stores the files. The app uses presigned URLs for downloads so users never need AWS keys.

IAM: The EC2 instance has an IAM role. The app gets credentials from the instance metadata service. No keys are stored in the code.

## Requirements

You need:

- An EC2 instance (Ubuntu or Debian). Something like t3.medium (2 vCPU, 4 GB RAM) is enough. It needs systemd.
- An IAM role on that instance with permission to read from the bucket: `s3:GetObject` and `s3:ListBucket` on `kgx-translator-ingests`.
- An Elastic IP so the instance has a fixed public IP.
- The domain kgx-storage.ci.transltr.io pointing at that IP (DNS A record).
- Security group open for: 22 (SSH), 80 (HTTP, for certbot and redirect), 443 (HTTPS).

Software on the server:

- Python 3.12.3 (see .python-version). Same version everywhere keeps things predictable.
- Nginx (reverse proxy and SSL).
- Certbot (gets and renews Let's Encrypt certificates).
- Python packages from requirements.txt (versions are pinned).

## Installation (Fresh EC2)

### 1. Clone the repo

```bash
cd /home/ubuntu
git clone https://github.com/NCATSTranslator/translator-ingests-ui.git kgx-storage-webserver
cd kgx-storage-webserver
```

This puts the code in `/home/ubuntu/kgx-storage-webserver`. The systemd service expects that path.

### 2. Install system packages

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx python3.12 python3.12-venv python3-pip
```

You get Nginx, Certbot, Python 3.12, venv, and pip.

### 3. Python virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

This keeps the app’s dependencies separate from the system. All installs go into `.venv`.

### 4. Install and start the web service

```bash
cd /home/ubuntu/kgx-storage-webserver
sudo ./setup-webserver-service.sh
sudo systemctl status kgx-storage-webserver
```

The script copies the systemd unit file and starts the service. Check status to make sure it’s running.

### 5. Configure Nginx

```bash
sudo cp nginx-config /etc/nginx/sites-available/kgx-storage
sudo ln -sf /etc/nginx/sites-available/kgx-storage /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx
```

Nginx now proxies to the Flask app. The default site is removed so port 80 is free. `nginx -t` checks the config before you restart.

### 6. HTTPS with Let's Encrypt

```bash
sudo certbot --nginx -d kgx-storage.ci.transltr.io
sudo certbot renew --dry-run
```

Certbot gets a certificate and configures Nginx. The dry run checks that renewal will work later.

### 7. Security group

In the AWS console, open these ports:

| Type  | Port | Source    | Why |
|-------|------|-----------|-----|
| SSH   | 22   | Your IP   | So you can log in |
| HTTP  | 80   | 0.0.0.0/0 | Certbot and redirect to HTTPS |
| HTTPS | 443  | 0.0.0.0/0 | The actual site |

Restrict SSH to your IP if you can.

### 8. Generate metrics (faster browsing)

```bash
cd /home/ubuntu/kgx-storage-webserver
source .venv/bin/activate
python compute_metrics.py
```

This builds `metrics.json` with folder sizes and file counts. The web app reads this so it doesn’t have to ask S3 for every folder. Run it once after setup. You can also run `update_metrics.sh` from cron (for example every hour) to refresh it.

### 9. Check that it works

```bash
curl -I https://kgx-storage.ci.transltr.io
```

You want a 200 or 302. That means Nginx, SSL, and the app are all working.

## File structure

What’s in the repo:

- `web_server.py` – Flask app. Routes, S3 calls, and the HTML for the browser, JSON viewer, and docs live here.
- `compute_metrics.py` – Script that scans the bucket and writes folder stats to `metrics.json`.
- `update_metrics.sh` – Runs compute_metrics.py and sends HUP to Gunicorn so workers reload. Use in cron.
- `metrics.json` – Created by compute_metrics.py. Not in git. Makes folder listing fast.
- `requirements.txt` – Python dependencies (pinned versions).
- `.python-version` – Says to use Python 3.12.3.
- `kgx-storage-webserver.service` – Systemd unit. Installed by setup-webserver-service.sh.
- `setup-webserver-service.sh` – Installs the service and starts it.
- `nginx-config` – Copy this to Nginx’s sites-available.
- `.gitignore` – Tells git what not to track.
- `public/` – Static files (e.g. ncats-banner.png, favicon.png). Served by the app.
- `README.md` – This file.

## Service management

The app runs as a systemd service named `kgx-storage-webserver`.

Check status:
```bash
sudo systemctl status kgx-storage-webserver
```

Stop, start, or restart:
```bash
sudo systemctl stop kgx-storage-webserver
sudo systemctl start kgx-storage-webserver
sudo systemctl restart kgx-storage-webserver
```

Watch logs live:
```bash
sudo journalctl -u kgx-storage-webserver -f
```

Last 100 lines:
```bash
sudo journalctl -u kgx-storage-webserver -n 100
```

Turn on or off start at boot:
```bash
sudo systemctl enable kgx-storage-webserver
sudo systemctl disable kgx-storage-webserver
```

Nginx: After editing config, test then reload:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

Certificates: They renew automatically. To check or force renew:
```bash
sudo certbot certificates
sudo certbot renew
sudo certbot renew --dry-run
```

## Log files

- App: `/var/log/kgx-storage/access.log` and `error.log`
- Service: `sudo journalctl -u kgx-storage-webserver`
- Nginx: `/var/log/nginx/access.log` and `error.log`

## Deploying code changes

```bash
cd /home/ubuntu/kgx-storage-webserver
git pull
sudo systemctl restart kgx-storage-webserver
sudo systemctl status kgx-storage-webserver
```

Python changes need a restart. Nginx config changes need `sudo nginx -t` then `sudo systemctl reload nginx`.

## Troubleshooting

**Service won’t start**

Look at the logs:
```bash
sudo journalctl -u kgx-storage-webserver -n 50
```

See if something else is on port 5000:
```bash
sudo ss -tulpn | grep 5000
```

Check that Python can load the app:
```bash
cd /home/ubuntu/kgx-storage-webserver
source .venv/bin/activate
python -c "import flask, boto3, gunicorn"
```

**502 Bad Gateway**

Nginx can’t reach the app. Usually the app isn’t running. Run `sudo systemctl status kgx-storage-webserver` and `sudo journalctl -u kgx-storage-webserver -n 50` to see why.

**SSL problems**

Check certs: `sudo certbot certificates`. Test Nginx: `sudo nginx -t`. Look at the Nginx site config if something is wrong.

**JSON opens as download instead of viewer**

Restart the app so it picks up the routes: `sudo systemctl restart kgx-storage-webserver`.

**S3 Access Denied**

The instance probably doesn’t have an IAM role or the role can’t read the bucket. Check:
```bash
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/
```
If that returns a role name, the role is attached. Then in AWS make sure that role has `s3:GetObject` and `s3:ListBucket` on the `kgx-translator-ingests` bucket.

## IAM permissions

The EC2 instance role needs this policy (or equivalent):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::kgx-translator-ingests",
        "arn:aws:s3:::kgx-translator-ingests/*"
      ]
    }
  ]
}
```

- `s3:GetObject` on the bucket’s objects: read files (for downloads and JSON viewer).
- `s3:ListBucket` on the bucket: list prefixes so we can show folders.

No write actions. Credentials come from the instance metadata, not from the code.

## Development

To run the app locally without Nginx or systemd:

```bash
cd translator-ingests-ui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Docs page only** (no S3 needed):

```bash
python web_server.py
# open http://localhost:5000/docs
```

**Full browse UI** (folder listings, translator_kg banner, JSON viewer):

The ITRB CI bucket `kgx-translator-ingests` is not publicly listable. Configure AWS credentials with `s3:GetObject` and `s3:ListBucket` on that bucket, then:

```bash
PORT=5001 python web_server.py
```

Then open http://localhost:5001/. Use `PORT=5001` on macOS if port 5000 is taken by AirPlay Receiver.

Paths to spot-check UI changes:

- http://localhost:5001/docs
- http://localhost:5001/releases/translator_kg/
- http://localhost:5001/releases/translator_kg_open/

`KGX_ANONYMOUS_S3=1` is only for buckets with a public-read policy (not `kgx-translator-ingests`). This dev server is single-threaded and not for production.

## Production URLs and downloads

Site: https://kgx-storage.ci.transltr.io

- Home: https://kgx-storage.ci.transltr.io
- Folders: https://kgx-storage.ci.transltr.io/releases/alliance/latest/ (and similar paths; trailing slash lists the folder)
- File (canonical): https://kgx-storage.ci.transltr.io/releases/alliance/latest/graph-metadata.json — returns the file (JSON as response body, other types trigger download)
- JSON viewer: same path with `?view`, e.g. https://kgx-storage.ci.transltr.io/releases/alliance/latest/graph-metadata.json?view — shows the HTML viewer
- Docs: https://kgx-storage.ci.transltr.io/docs

Old links with `?path=...` redirect to the path-style URL. Legacy `/view/` and `/download/` URLs are no longer supported (404).

**Downloading without AWS**

Use the canonical file URL with curl or wget. Always use the `-fL` flags with curl to ensure reliable downloads:

```bash
curl -fL -O "https://kgx-storage.ci.transltr.io/releases/alliance/latest/alliance.tar.zst"
```

The flags:
- `-L` follows HTTP redirects (required for the server's routing)
- `-f` fails on HTTP errors (prevents saving error pages as files)
- `-O` saves with the remote filename

Without `-L`, curl saves redirect responses instead of the actual file. Without `-f`, curl silently saves HTTP error pages (404, 500, etc.) as if they were valid data, which can corrupt your analysis pipeline.

For wget, redirects are followed by default and errors return non-zero exit codes, so the basic command is sufficient:

```bash
wget "https://kgx-storage.ci.transltr.io/releases/alliance/latest/alliance.tar.zst"
```

Examples and more commands are on the /docs page.

**Downloading with AWS CLI**

If you have credentials that can read the bucket, you can use `aws s3 cp` and `aws s3 sync` on `s3://kgx-translator-ingests/`. See the docs page for paths and examples.

**Example paths in the bucket**

- releases/alliance/latest/alliance.tar.zst
- releases/alliance/latest/graph-metadata.json
- releases/reactome/latest/ (and similar)

**Metadata and external consistency**

Use the canonical URL format for any reference to kgx-storage files: path only (e.g. `https://kgx-storage.ci.transltr.io/releases/alliance/latest/graph-metadata.json`), with optional `?view` for the JSON viewer. Metadata files (e.g. graph-metadata.json), DAWG, or other systems that publish or consume kgx-storage URLs should use this format so "URL in metadata" matches "URL in app" and links work the same everywhere. For version checks (e.g. "is there a new release?"), using metadata (e.g. `release_version` in latest-release.json or `url`/`id` in graph-metadata.json) is more reliable than relying only on the Last-Modified HTTP header.

**Edge cases**

- Path that is neither an S3 object nor a prefix (e.g. typo): 404, same HTML response as reserved paths like `/view` or `/download`.
- Directory path without trailing slash (e.g. `/releases/alliance/latest`): redirect to the same path with trailing slash so the folder listing is shown.
- Query parameters: only `?view` is significant for JSON viewer; other params (e.g. `?foo=bar`) are ignored. Redirects use the canonical path with no query string.

## Related repos

Translator-ingests (pipeline that writes the data): https://github.com/NCATSTranslator/translator-ingests

## Security

- HTTPS with Let's Encrypt. Traffic is encrypted.
- No AWS keys in the app. The instance role is used via metadata.
- Download links are presigned and expire in 1 hour.
- The Flask app only listens on the localhost. Only Nginx talks to it from the outside.
- S3 access is read-only. Nobody can change or delete data through this app.
- There is no rate limiting. If you need it, you can add it in Nginx.

## License

Part of the NCATS Biomedical Data Translator project.
