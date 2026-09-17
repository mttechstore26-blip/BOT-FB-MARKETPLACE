import time
from playwright.sync_api import sync_playwright

FACEBOOK_STATE = "facebook_state.json"


def _first_visible(locator):
    try:
        for i in range(locator.count()):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                pass
    except Exception:
        pass
    return None


def send_facebook_dm(item_url, message):
    if not message or not message.strip():
        return False, "Messaggio vuoto"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            storage_state=FACEBOOK_STATE,
            viewport={"width": 1400, "height": 1000},
        )

        page = context.new_page()

        try:
            page.goto(
                item_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            time.sleep(4)

            current_url = page.url.lower()

            if (
                "/login" in current_url
                or "/checkpoint" in current_url
                or "login.php" in current_url
            ):
                return False, "Sessione Facebook non valida"

            # -------------------------------------------------
            # APRI IL POPUP MESSAGGIO
            # -------------------------------------------------
            open_button = _first_visible(
                page.locator(
                    '[role="button"][aria-label="Invia un messaggio al venditore"]'
                )
            )

            if open_button is None:
                return False, "Pulsante 'Invia un messaggio al venditore' non trovato"

            open_button.focus()
            page.keyboard.press("Enter")
            time.sleep(2)

            # -------------------------------------------------
            # TEXTAREA
            # -------------------------------------------------
            textarea = _first_visible(page.locator("textarea"))

            if textarea is None:
                return False, "Textarea messaggio non trovata"

            try:
                textarea.fill(message)
            except Exception:
                textarea.click()
                page.keyboard.press("Control+A")
                page.keyboard.type(message)

            time.sleep(1)

            # -------------------------------------------------
            # PULSANTE FINALE "INVIA MESSAGGIO"
            # -------------------------------------------------
            send_button = _first_visible(
                page.locator(
                    '[role="button"][aria-label="Invia messaggio"]'
                )
            )

            if send_button is None:
                return False, "Pulsante finale 'Invia messaggio' non trovato"

            send_button.focus()
            page.keyboard.press("Enter")
            time.sleep(2)

            return True, "Messaggio inviato"

        except Exception as exc:
            return False, str(exc)

        finally:
            browser.close()
