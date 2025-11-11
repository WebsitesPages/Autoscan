# Overview

Autoscan is a web-based car listing aggregator that scrapes automotive marketplaces (primarily Kleinanzeigen.de, with additional support for AutoScout24 and Carwow) and displays them in a centralized dashboard. The application periodically scrapes listings, stores them in a SQLite database, tracks price history, and sends push notifications when new listings appear.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Application Structure

**Web Framework**: Flask-based single-file application (`app.py`) serving a web UI for browsing aggregated car listings. The application uses server-side rendering with Jinja2 templates (inline template strings) rather than separate template files.

**Database Layer**: SQLite with WAL mode (`autos.db`) for concurrent read/write access. The database schema includes:
- `listings` table: Core listing data (id, platform, price, mileage, location, brand, model, fuel type, etc.)
- `listing_prices` table: Price history tracking (referenced but not shown in full schema)
- Push notification subscriptions table (initialized but schema not shown)

**Scraping Architecture**: Separate scraper modules run independently from the web server:
- `scrape_ebay.py`: Main scraper for Kleinanzeigen.de (note: filename is legacy, actually scrapes Kleinanzeigen)
- `runner.py`: Simple scheduler that runs scrapers every 30 minutes
- Scrapers use requests + BeautifulSoup for HTML parsing

**Provider Modules** (`providers/` directory):
- `links.py`: URL builders for search queries across platforms
- `ka_stats.py`: Kleinanzeigen statistics fetcher (listing counts, average prices)
- `autoscout_stats.py`: AutoScout24 statistics fetcher
- `carwow_stats.py`: Carwow statistics fetcher

## Key Design Decisions

**Monolithic Web App**: Single `app.py` file contains all routes and inline HTML templates. This simplifies deployment but sacrifices modularity. The approach works well for small-scale personal projects but would need refactoring for larger teams.

**No Caching Strategy**: Explicit cache-busting headers on all responses (`Cache-Control: no-store`) ensures users always see fresh data. The service worker (`static/sw.js`) also bypasses caching entirely, passing all requests directly to the network.

**Regional Configuration via Environment Variables**: Search parameters (geographic area, radius, price ranges) are configurable through environment variables:
- `KA_AREA_SLUG`, `KA_AREA_CODE`: Geographic region targeting
- `KA_RADIUS`: Search radius in kilometers
- `KA_PRICE_MIN`, `KA_PRICE_MAX`, `KA_KM_MAX`: Listing filters

**Pagination**: Query parameter-based pagination (`?page=N&per_page=M`) with configurable items per page (default 50).

**Data Freshness Tracking**: Listings track `first_seen` and `last_seen` timestamps plus a `status` field to distinguish active vs. expired listings.

## Security & Performance

**VAPID Keys for Push Notifications**: Uses elliptic curve cryptography (SECP256R1) for Web Push API authentication. Keys are generated via `tools/gen_vapid.py` and provided as environment variables.

**Rate Limiting**: No explicit rate limiting implemented. Scrapers run on fixed 30-minute intervals, which provides natural throttling.

**Error Handling**: Scrapers use try/except blocks and continue on failures rather than crashing. HTTP requests have 12-second timeouts.

# External Dependencies

## Third-Party Services

**Kleinanzeigen.de** (formerly eBay Kleinanzeigen): Primary data source for car listings. The scraper parses HTML pages directly as no official API is available. Prone to breaking if the website structure changes.

**AutoScout24**: Secondary marketplace for comparative statistics. HTML scraping with pagination support (up to 2 pages).

**Carwow**: Tertiary marketplace integration for price comparisons. Includes bot detection mitigation (checks for CAPTCHA/blocking messages).

## Key Python Libraries

**Web Framework**:
- `Flask 3.1.2`: Web server and routing
- `Jinja2 3.1.6`: Template rendering (inline templates)

**Scraping Stack**:
- `requests 2.32.5`: HTTP client
- `beautifulsoup4 4.14.2` + `lxml 6.0.2`: HTML parsing
- `aiohttp 3.13.2`: Async HTTP (likely unused based on visible code)

**Push Notifications**:
- `pywebpush 2.1.2`: Web Push Protocol implementation
- `py-vapid 1.9.2`: VAPID key generation/handling
- `cryptography 46.0.3`: Cryptographic operations for VAPID

**Database**: 
- `sqlite3` (Python standard library): No ORM, direct SQL queries with parameterized statements

## Environment Variables

Required for operation:
- `VAPID_PUBLIC_KEY`: Base64-encoded public key for Web Push
- `VAPID_PRIVATE_KEY_PEM`: PEM-encoded private key
- `PUSH_SUBJECT`: Mailto URI for push notification identification

Optional configuration:
- `AUTOS_DB`: Database file path (default: `autos.db`)
- `KA_AREA_SLUG`, `KA_AREA_CODE`, `KA_RADIUS`: Search region settings
- `KA_PRICE_MIN`, `KA_PRICE_MAX`, `KA_KM_MAX`: Listing filters

## Notable Constraints

**No Official APIs**: All marketplace integrations rely on HTML scraping, making them fragile and dependent on stable website structures. Bot detection and CAPTCHA challenges are ongoing risks.

**SQLite Limitations**: WAL mode improves concurrent access but SQLite remains single-file, limiting horizontal scaling. Suitable for single-instance deployments only.

**Synchronous Scraping**: Despite including `aiohttp` in dependencies, the scraper uses synchronous requests. This limits throughput but simplifies error handling.