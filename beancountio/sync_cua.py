import csv
import hashlib
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger  # type: ignore[import-not-found]

from beancountio.config import CUASource, SecretRef

CUA_BASE = "https://online.cua.com"
SECURED_BASE = "https://secured.cua.com"

TRANSACTIONS_URL = "/Security/Transactions/Transactions.aspx?trxid=TRXC0101160"
ACCOUNT_ID_FIELD = "MainContent_TransactionMainContent_accControl_hdnSelectedAccountId"

EXPORT_LI = "#MainContent_TransactionMainContent_txpTransactions_ctl01_proofControl_li_ex2"
EXPORT_DROPDOWN = "#MainContent_TransactionMainContent_txpTransactions_ctl01_proofControl_ddlExportType_dlField"


def _resolve(val: str | SecretRef) -> str:
    return val.resolve() if isinstance(val, SecretRef) else val


def safe_filename(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s)[:60].strip("_")


def quarter_subdir(base: Path, dt: datetime) -> Path:
    q = (dt.month - 1) // 3 + 1
    return base / f"{dt.year}-Q{q}"


def split_csv(csv_text: str, output_base: Path) -> tuple[int, int]:
    reader = csv.DictReader(io.StringIO(csv_text))
    new, skipped = 0, 0
    for row in reader:
        date_str = row["Date"].strip()
        description = row["Description"].strip()
        amount = row["Amount"].strip()
        dt = datetime.strptime(date_str, "%b %d, %Y")
        uid = hashlib.sha1(f"{date_str}|{description}|{amount}".encode()).hexdigest()[:10]
        slug = safe_filename(description.split(",")[0])
        filename = f"{dt.strftime('%Y-%m-%d')}_{slug}_{uid}.txt"
        dest = quarter_subdir(output_base, dt)
        out = dest / filename
        if out.exists():
            skipped += 1
            continue
        dest.mkdir(parents=True, exist_ok=True)
        out.write_text(f"Date: {dt.strftime('%Y-%m-%d')}\nDescription: {description}\nAmount: {amount}\n")
        logger.info("Saved {}", out)
        new += 1
    return new, skipped


def fetch_batch(sources: list[CUASource], headed: bool = False, since: date | None = None) -> None:
    """Download transactions for multiple CUA accounts in one browser session.

    since overrides the per-source 'days' config.
    """
    from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout  # type: ignore[import-not-found]
    from playwright_stealth.stealth import Stealth  # type: ignore[import-not-found]

    def login(page: Page, user: str, password: str) -> None:
        logger.info("Navigating to CUA login...")
        page.goto(f"{CUA_BASE}/")
        page.wait_for_url(f"{SECURED_BASE}/**", timeout=15_000)
        page.wait_for_selector('input[name="Username"]', timeout=15_000)
        page.fill('input[name="Username"]', user)
        page.keyboard.press("Enter")
        page.wait_for_selector('input[name="Password"]', timeout=15_000)
        page.fill('input[name="Password"]', password)
        page.keyboard.press("Enter")
        page.wait_for_url(f"{CUA_BASE}/**", timeout=30_000)
        logger.info("Login successful")

    def fetch_csv(context, date_from: date, date_to: date, account_name: str) -> str:
        fmt = "%b %-d, %Y"
        tab = context.new_page()
        try:
            tab.goto(f"{CUA_BASE}{TRANSACTIONS_URL}")
            tab.get_by_text(account_name, exact=True).click()
            tab.wait_for_selector(EXPORT_LI, state="visible", timeout=30_000)
            tab.evaluate(
                """([from_date, to_date]) => {
                    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
                    set('hdnExportDateFrom', from_date);
                    set('hdnExportDateTo', to_date);
                }""",
                [date_from.strftime(fmt), date_to.strftime(fmt)],
            )
            tab.click(EXPORT_LI)
            tab.wait_for_selector("#divPrintCommands_100", state="visible")
            tab.select_option(EXPORT_DROPDOWN, "CSV")
            with tab.expect_download(timeout=30_000) as dl:
                tab.click("#btnExport")
            return Path(dl.value.path()).read_text(encoding="utf-8-sig")
        finally:
            tab.close()

    first = sources[0]
    user = _resolve(first.cua_user)
    password = _resolve(first.cua_password)

    with sync_playwright() as p:
        Stealth().hook_playwright_context(p)
        browser = p.firefox.launch(headless=not headed, slow_mo=500 if headed else 0)
        context = browser.new_context()
        login_page = context.new_page()
        try:
            login(login_page, user, password)
            for source in sources:
                account_name = source.account_name
                date_to = date.today()
                date_from = since if since else date_to - timedelta(days=source.days)
                logger.info("Fetching {} (from {})...", source.name, date_from)
                try:
                    csv_text = fetch_csv(context, date_from, date_to, account_name)
                    new, skipped = split_csv(csv_text, source.source_dir)
                    logger.info("{}: {} new, {} already existed", source.name, new, skipped)
                except PlaywrightTimeout as exc:
                    logger.error("Timed out on {}: {}", source.name, exc)
        except PlaywrightTimeout as exc:
            logger.error("Timed out during login: {}", exc)
        finally:
            browser.close()
