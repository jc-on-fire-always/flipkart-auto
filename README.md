# 24/7 Flipkart Restock Monitor

A resilient, production-grade 24/7 background Python automation daemon that continuously monitors out-of-stock items on Flipkart, detects when purchasability transitions to **IN STOCK**, and immediately sends alert notifications with direct clickable links via **WhatsApp** (official Meta WhatsApp Cloud API) with automatic **Email** (authenticated SMTP) fallback.

---

## 1. Architecture Overview

```
                      +---------------------------------------+
                      |       24/7 Monitoring Daemon          |
                      |     (app.monitor.FlipkartMonitor)     |
                      +-------------------+-------------------+
                                          |
                        +-----------------+-----------------+
                        |                                   |
                        v                                   v
             [ Stock Providers ]                 [ Persistence & Health ]
    +---------------------------------+      +---------------------------------+
    | - MockStockProvider (Test/Demo) |      | - SQLite (WAL mode) / PostgreSQL|
    | - FlipkartAffiliateStockProvider|      | - Deduplication & Cooldowns     |
    | - FlipkartWebStockProvider      |      | - Lightweight HTTP /health (8080|
    +----------------+----------------+      +----------------+----------------+
                     |                                        |
                     +-------------------+--------------------+
                                         |
                       Restock Transition: OUT_OF_STOCK -> IN_STOCK
                                         |
                                         v
                            [ Notification Service ]
              +--------------------------+--------------------------+
              |                                                     |
              v                                                     v
      [ Primary Channel ]                                  [ Fallback Channel ]
   Meta WhatsApp Cloud API                             Authenticated SMTP Email
 (Approved Template Messages)                             (Plain Text + HTML)
```

### Key Architectural Tenets
1. **Zero Purchasing / Payment Automation**: The system *never* accesses your bank account, *never* stores UPI PINs or OTPs, and *never* executes checkout. It alerts you immediately with direct clickable links so you can inspect the official Flipkart listing and finalize payment yourself.
2. **Defensive Availability Detection**: `UNKNOWN` availability or network timeouts are **never** treated as `IN_STOCK`. Prior valid states are never corrupted by temporary network failures.
3. **Multi-Channel Fallback**: If WhatsApp encounters an API error or token issue, alert dispatch automatically falls back to authenticated Email (Gmail SMTP / SendGrid).
4. **Debouncing & Deduplication**: Configurable verification counts debounce temporary inventory flickers, while database-backed cooldowns prevent duplicate notification spam.
5. **Security by Design**: Automated secret redaction in logs, SSRF domain restriction (`*.flipkart.com`), and non-root container isolation (UID 1000).

---

## 2. Availability Sources & Technical Analysis

### Source Comparison & Status

| Source Adapter | Type | Requirements | Purchasability Evidence | Limitations & Status |
| :--- | :--- | :--- | :--- | :--- |
| **`mock`** | Deterministic Simulator | None | Programmed state sequence | For development, unit testing, and verifying notifications end-to-end. |
| **`flipkart_affiliate`** | Official Partner API | `FLIPKART_AFFILIATE_ID`<br>`FLIPKART_AFFILIATE_TOKEN` | Direct JSON feed attributes:<br>`productBaseInfoV1.inStock`<br>`isAvailable`<br>`flipkartSellingPrice` | **Recommended partner source**. Requires an approved Flipkart Affiliate publisher account. |
| **`flipkart_web`** *(Default Live)* | Direct Listing Inspector | Public Web Access | Server-rendered `window.__INITIAL_STATE__` parsing (`ATLAS_PRODUCT_PRICING_SUMMARY`, `ATLAS_PRODUCT_ACTION`), Schema.org JSON-LD, purchasability buttons (`BUY NOW`), live price. | Subject to Akamai bot management. If challenged, safely returns `UNKNOWN` (never false restock). Filters sponsored ads. |

### Availability Detection Details

- **Flipkart Web Inspector (`flipkart_web`)**:
  - **Single-Pass State Parsing**: Inspects server-rendered `window.__INITIAL_STATE__` without requiring heavy browser automation or headless Chrome.
  - **Accurate Pricing**: Extracts real product prices directly from the `ATLAS_PRODUCT_PRICING_SUMMARY` widget data and official price classes (`Nx9bqj CxhGGd`), strictly ignoring sponsored ad banners (`LAS_ADVERTISEMENT_...`) that appear at the top of pages.
  - **Title Sanitization**: Extracts clean titles from the `ATLAS_PRODUCT_TITLE` widget and strips server hydration placeholders like `{TITLE}` using your configured `PRODUCT_NAME`.
  - **Bot Protection Gracefulness**: If an Akamai security challenge or HTTP 403/429 is encountered, the inspector logs a warning and returns `UNKNOWN` with confidence `0.0`. It does **not** attempt circumvention and never triggers false alerts.

---

## 3. WhatsApp Business Notifications (Meta Cloud API)

The WhatsApp integration utilizes Meta's official WhatsApp Business Cloud API. **No unofficial browser automation, QR-code session hijacking, or personal account scrapers are used.**

### Message Format (Template Mode)

```text
FLIPKART RESTOCK ALERT

Product: PUMA ST Runner v4 SD Sneakers For Men (Grey , 6)

Status: Reported IN STOCK

Detected at: 2026-10-10 17:30:00 UTC

Price: ₹2,226.00

Open the product here:
https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c?pid=SHOHDMPEQASZEZA3

Availability may change before checkout. Open the official listing to verify the item, delivery eligibility, and final price.
```

### Setup Guide for Meta WhatsApp Cloud API

#### 1. App Creation
1. Go to [Meta for Developers](https://developers.facebook.com/) and register or log in.
2. Navigate to **My Apps** &rarr; **Create App** &rarr; Select **Other** &rarr; Select **Business**.
3. Under App Dashboard, find **WhatsApp** and click **Set up**.
4. In **WhatsApp &rarr; API Setup**:
   - Note your **Phone Number ID** (e.g., `1366519953212365`).
   - Add your recipient phone number to the **To** field (up to 5 numbers allowed in Development Mode).

#### 2. Generate a Permanent System User Token (Never Expires)
*Note: Temporary access tokens from the API Setup screen expire in 24 hours. For 24/7 cloud deployment, generate a permanent System User token:*
1. Go to [Meta Business Settings](https://business.facebook.com/settings).
2. Under **Users**, click **System Users** &rarr; click **Add** (Name: `FlipkartMonitorBot`, Role: `Admin`).
3. Click **Add Assets** &rarr; **Apps** &rarr; Select your monitor app &rarr; enable **Full Control**.
4. Click **Generate New Token**:
   - Select your App.
   - Token expiration: Select **Never**.
   - Permissions: Check `whatsapp_business_messaging` and `whatsapp_business_management`.
5. Copy the generated permanent token into `WHATSAPP_ACCESS_TOKEN` in your `.env`.

> [!NOTE]
> **No App Publishing Required:** For personal alerts, keep your app in **Development Mode** (unpublished). Meta permits full 24/7 API usage for numbers added to your verified test/allowed recipient list without business verification or review.

#### 3. Message Templates
Meta requires business-initiated messages to use an approved message template:

- **Instant Zero-Setup Option (`hello_world`)**:
  Meta includes a pre-approved default template named `hello_world`. To test immediately without waiting for template reviews:
  ```env
  WHATSAPP_TEMPLATE_NAME=hello_world
  WHATSAPP_TEMPLATE_LANGUAGE=en_US
  ```

- **Custom Restock Template (`flipkart_restock_alert`)**:
  In [Meta WhatsApp Manager &rarr; Message Templates](https://business.facebook.com/wa/manage/message-templates/):
  - Category: `Utility`
  - Name: `flipkart_restock_alert`
  - Language: `English (US)` (`en_US`)
  - Body:
    ```text
    FLIPKART RESTOCK ALERT

    Product: {{1}}
    Status: Reported IN STOCK
    Detected at: {{2}}
    Price: {{3}}

    Open the product here:
    {{4}}

    Availability may change before checkout. Open the official listing to verify the item, delivery eligibility, and final price.
    ```
  - Provide sample variables for review: `{{1}}` (Product Name), `{{2}}` (Timestamp), `{{3}}` (Price), `{{4}}` (Link). Approval takes 1–15 minutes.

---

## 4. Email Notifications (Authenticated SMTP)

The email integration supports authenticated SMTP with STARTTLS or SSL, generating clean plain-text messages along with rich HTML emails featuring prominent clickable buttons and direct links.

### Configuration Guide

#### Using Gmail SMTP
1. Enable **2-Step Verification** on your Google Account.
2. Go to **Security &rarr; 2-Step Verification &rarr; App passwords**.
3. Generate an App Password for "Flipkart Monitor".
4. Configure `.env`:
   ```env
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=your_email@gmail.com
   SMTP_PASSWORD=your_16_char_app_password
   SMTP_FROM=your_email@gmail.com
   SMTP_USE_TLS=true
   EMAIL_TO=recipient@example.com
   ```

---

## 5. Local Setup & Testing

### 1. Installation
```bash
# Clone the repository
git clone <repo-url> flipkart-monitor
cd flipkart-monitor

# Create and activate virtual environment
python -m venv .venv

# On Windows PowerShell:
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Run Automated Tests
```bash
# Run complete test suite (33 tests covering multi-product batching, transitions, debouncing, retries, SSRF, & health)
pytest -v
```

### 3. Multi-Product Management & CLI Commands

You can track **any number of products simultaneously**. The monitor reads from `products.json` with polite request staggering (configurable `REQUEST_STAGGER_SECONDS=2.0` with jitter) to prevent Flipkart rate-limiting, and tracks inventory states, baseline confirmations, and price ceilings independently per product.

```bash
# 1. List all currently configured monitored products
python -m app.cli list-products

# 2. Add a new product to monitor with optional price ceiling
python -m app.cli add-product --name "PUMA Sneakers" --url "https://www.flipkart.com/..." --max-price 2500.0

# 3. Remove a product by index number, name, or URL
python -m app.cli remove-product 1

# 4. Run a live stock check on all configured products (or target a specific one with -p)
python -m app.cli check-once
python -m app.cli check-once -p 1
python -m app.cli check-once -p "PUMA"

# 5. Inspect database health, tracked product states, and restock event history
python -m app.cli status

# 6. Test notification channels (dispatches diagnostic alerts to WhatsApp & Email)
python -m app.cli test-notifications

# 7. Simulate a live restock event (triggers full restock alert format immediately)
python -m app.cli simulate-restock

# 8. Start continuous 24/7 background monitor daemon
python -m app.cli run
```

#### `products.json` Format
If you prefer editing the file directly, `products.json` resides in the project root:
```json
[
  {
    "name": "PUMA ST Runner v4 SD Sneakers For Men",
    "url": "https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c?pid=SHOHDMPEGCHTGP52...",
    "max_price": 2500.0,
    "enabled": true
  },
  {
    "name": "NUMERO UNO Men Loose Fit Mid Rise Blue Jeans",
    "url": "https://www.flipkart.com/numero-uno-loose-fit-men-blue-jeans/p/itm0d72205b89486?pid=JEAHPHDNH7HSNGFZ...",
    "max_price": 1000.0,
    "enabled": true
  }
]
```
*(If `products.json` is absent, the monitor seamlessly falls back to the single `PRODUCT_NAME` and `PRODUCT_URL` configured in `.env`)*.

---

## 6. Cloud Deployment Walkthrough (Google Cloud VM / Linux VPS)

Deploy the application onto any Linux cloud instance (Google Cloud Platform, AWS EC2, DigitalOcean, Hetzner) using Docker.

### Step 1: Install Docker & Docker Compose
```bash
# Update packages and install Docker
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
newgrp docker

# Verify installation
docker compose version
```

### Step 2: Upload or Clone the Project
```bash
mkdir -p ~/flipkart-monitor
cd ~/flipkart-monitor

# Clone or pull your repository:
git clone <repo-url> .
```

### Step 3: Configure Environment
```bash
cp .env.example .env
nano .env

# Lock file permissions so only your user can read credentials
chmod 600 .env
```

### Step 4: Validate Setup
```bash
# Test notification delivery from the VM
docker compose run --rm flipkart-monitor test-notifications

# Test live stock inspection for your product
docker compose run --rm flipkart-monitor check-once
```

### Step 5: Start 24/7 Background Daemon
```bash
# Launch container in detached mode with auto-restart enabled
docker compose up -d --build

# Verify container is running and healthy
docker compose ps
```

### Step 6: Live Monitoring & Health Inspection
```bash
# Follow logs in real time
docker compose logs -f flipkart-monitor

# Query internal health daemon
curl -s http://127.0.0.1:8080/health | jq .
```

Expected health JSON:
```json
{
  "status": "healthy",
  "service_running": true,
  "uptime_seconds": 3600,
  "last_successful_stock_check": "2026-10-10T17:30:00Z",
  "last_observed_availability": "OUT_OF_STOCK",
  "last_successful_notification": null,
  "consecutive_errors": 0,
  "last_error_summary": null,
  "active_provider": "flipkart_web_inspector",
  "checked_at_utc": "2026-10-10T17:30:01Z"
}
```

---

## 7. Cloud Operational Cheatsheet

| Task | Command on VM |
| :--- | :--- |
| **View Live Monitor Logs** | `docker compose logs -f flipkart-monitor` |
| **Check Container Status** | `docker compose ps` |
| **List Monitored Products** | `docker compose exec flipkart-monitor python -m app.cli list-products` |
| **Add New Product** | `docker compose exec flipkart-monitor python -m app.cli add-product --name "..." --url "..."` |
| **Inspect DB & State** | `docker compose exec flipkart-monitor python -m app.cli status` |
| **Run a One-Time Live Check** | `docker compose run --rm flipkart-monitor check-once` |
| **Simulate Restock Alert** | `docker compose run --rm flipkart-monitor simulate-restock` |
| **Update / Pull New Code** | `git pull && docker compose up -d --build` |
| **Restart Daemon** | `docker compose restart` |
| **Stop Daemon** | `docker compose down` |

---

## 8. Process Restart vs Service Availability

| Feature | How It Works |
| :--- | :--- |
| **Automatic Process Restart** | Configured with `restart: always` and `init: true` in `compose.yaml`. If the VM reboots for cloud maintenance, Docker immediately relaunches the container upon boot. |
| **Zombie Process Reaping** | `init: true` runs Tini as PID 1, preventing defunct zombie processes during months-long runs. |
| **Actual Service Availability** | The container runs an internal HTTP daemon (`:8080/health`). Docker `HEALTHCHECK` monitors consecutive check successes, reporting `unhealthy` if network stalls persist. |
| **State Persistence** | SQLite runs with WAL (Write-Ahead Logging) mounted to `./data/monitor.db`. Product state, cooldown timers, and notification records persist across restarts. |

---

## 9. Security & Privacy Safeguards

- **No Credential Storage**: No Flipkart user accounts, passwords, session tokens, or payment credentials are stored.
- **No Payment Automation**: The tool exclusively detects stock and delivers an official product URL. You approve UPI transactions yourself in your banking app.
- **SSRF Prevention**: Product URLs are strictly validated at startup. Only official `*.flipkart.com` endpoints are permitted; arbitrary intranet or loopback endpoints are rejected.
- **Automated Log Redaction**: A custom `SensitiveDataFilter` scrubs access tokens (`Bearer EAAG...`), passwords, and API secrets from all console and log outputs.
- **Container Isolation**: Docker runs as a dedicated non-root user (`appuser`, UID 1000) matching standard Linux host volume permissions.
- **Secret Permissions**: `.env` is excluded in `.gitignore` and secured via `chmod 600`.

---

## 10. Live Stock Verification Checklist

Before taking the monitor live for mission-critical restocks:

- [ ] **Provider Selection**: Set `STOCK_PROVIDER_TYPE=flipkart_web` in `.env` for live direct web inspection (or `flipkart_affiliate` if using partner API).
- [ ] **Product URL**: Verify `PRODUCT_URL` opens the exact required product variant on `https://www.flipkart.com/`.
- [ ] **Permanent Access Token**: Generate a System User token with `Never` expiration so WhatsApp alerts don't fail after 24 hours.
- [ ] **Diagnostic Notification**: Run `python -m app.cli test-notifications` and confirm receipt of WhatsApp and Email test messages on your devices.
- [ ] **Restock Alert Simulation**: Run `python -m app.cli simulate-restock` to verify that product links and price formatting display correctly.
- [ ] **Clickable Link Check**: Open the test message on your phone and ensure the Flipkart link opens the official product page.
- [ ] **Cooldown**: Ensure `ALERT_COOLDOWN_SECONDS` is set (default: `3600`) to avoid duplicate alerts.
- [ ] **Health Check**: Confirm `curl http://127.0.0.1:8080/health` reports `"status": "healthy"`.
