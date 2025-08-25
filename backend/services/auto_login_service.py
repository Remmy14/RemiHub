# backend/services/auto_login_service.py
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Callable, Dict, List

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from backend.config import load_config

@dataclass
class AutoLoginResult:
    success: bool
    message: str
    logs: List[str]
    elapsed_sec: float

class AutoLoginService:
    def __init__(self) -> None:
        self._providers: Dict[str, Callable[[str, List[str]], None]] = {
            'fanduel': self._login_fanduel,
            # add 'espn': self._login_espn later
        }

    def authenticate(self, network: str, code: str) -> AutoLoginResult:
        t0 = time.time()
        logs: List[str] = []
        net = network.strip().lower()
        code = code.strip()
        print(f'Got code: {code}')

        try:
            if not net or not code:
                return AutoLoginResult(False, "Missing 'network' or 'code'.", logs, 0.0)
            if net not in self._providers:
                return AutoLoginResult(False, f"Unsupported network: {net}", logs, 0.0)

            logs.append(f"Starting auto-login for {net} with code '{code}'.")
            self._providers[net](code, logs)
            return AutoLoginResult(True, "Authentication submitted successfully.", logs, time.time()-t0)
        except Exception as e:
            logs.append(f"Error: {e!s}")
            return AutoLoginResult(False, f"Failed: {e!s}", logs, time.time()-t0)

    # ---------- helpers ----------

    def _new_driver(self) -> webdriver.Chrome:
        opts = Options()
        # opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--disable-dev-shm-usage')
        # Optional: persist cookies
        # opts.add_argument('--user-data-dir=C:\\remihub\\chrome_profile')
        return webdriver.Chrome(options=opts)

    # ---------- providers ----------

    def _login_fanduel(self, code: str, logs: List[str]) -> None:
        cfg = load_config('config/config.ini')
        fd = cfg.get('Auto Login.fanduel', {})

        activate_url = fd.get('activate_url')  # e.g. https://fanduelsportsnetwork.com/mvpd/connect
        username = fd.get('username')
        password = fd.get('password')
        provider = fd.get('provider')
        sel = fd.get('selectors', {})

        if not activate_url or not username or not password:
            raise ValueError("FanDuel config missing activate_url/username/password.")

        code_input_sel = sel.get('code_input', 'input[name="code"]')
        cont_btn_sel  = sel.get('continue_btn', 'button[type="submit"]')
        user_sel      = sel.get('username', 'input[name="username"]')
        pass_sel      = sel.get('password', 'input[name="password"]')
        login_btn_sel = sel.get('login_btn', 'button[type="submit"]')

        d = self._new_driver()
        wait = WebDriverWait(d, 2)
        try:
            logs.append(f"GET {activate_url}")
            d.get(activate_url)

            # 1) Enter TV code
            code_input = d.find_element(By.CSS_SELECTOR, code_input_sel)
            code_input.clear()
            code_input.send_keys(code)

            # The code has been input, click the submit button
            d.find_element(By.CSS_SELECTOR, cont_btn_sel).click()

            # 2) Wait for either an alert or the login form
            alert_text = None
            try:
                wait.until(EC.alert_is_present())
                alert = d.switch_to.alert
                alert_text = alert.text
                alert.accept()
                print(f"Activation returned alert: {alert_text!r}")
            except Exception:
                # No JS alert; continue
                print('Successfully entered code')
                pass

            if alert_text:
                raise ValueError(f"Activation failed: {alert_text}")

            logs.append("Provider selection screen detected; selecting Altafiber...")
            _select_tv_provider(d, wait, provider_name=provider, logs=logs)

            # Allow things to load
            time.sleep(3)
            
            # 3) Login (if prompted)
            try:
                username_input = d.find_element(By.CSS_SELECTOR, user_sel)
                password_input = d.find_element(By.CSS_SELECTOR, pass_sel)
                username_input.clear(); username_input.send_keys(username)
                password_input.clear(); password_input.send_keys(password)
                d.find_element(By.CSS_SELECTOR, login_btn_sel).click()
                logs.append("Submitted login form.")
            except Exception as e:
                logs.append(f"Login form not shown; assuming session already authenticated.\n{e}")

            logs.append("Flow completed (best-effort).")
        finally:
            time.sleep(3)
            d.quit()

        print(logs)


def _select_tv_provider(d, wait, provider_name: str, logs: list[str]) -> None:
    """
    Picks a TV provider on the FanDuel 'Connect TV Provider' screen.
    Strategy:
      1) If a tile with the provider is visible, click it.
      2) Otherwise, open the 'Search for more providers' dialog,
         type a query (e.g., 'alta'), and click the matching item.
    """
    pname = provider_name.strip().lower()

    def _contains_case_insensitive(text: str) -> str:
        # XPath that matches any element whose text contains `text` (case-insensitive)
        return f"//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"

    # 1) Try direct tile first (some show on the main grid)
    try:
        tile_xpath = _contains_case_insensitive(pname)
        tile = wait.until(EC.element_to_be_clickable((By.XPATH, tile_xpath)))
        # Make sure we didn’t accidentally match something outside the tiles grid:
        # If needed, narrow with an ancestor filter that looks like the tile container.
        tile.click()
        logs.append(f"Clicked provider tile: {provider_name}")
        return
    except Exception:
        logs.append(f"Provider tile not directly visible: {provider_name}; using search modal.")

    # 2) Open "Search for more providers"
    # Try the input box on the page; if it opens a modal, great. If not, click the link first.
    try:
        # If a link/button opens the modal
        try:
            btn = wait.until(EC.element_to_be_clickable((By.ID, "searchMoreIcon")))
            try:
                btn.click()
            except Exception:
                # Fallback if overlay intercepts the click
                d.execute_script("arguments[0].click();", btn)
            logs.append("Opened provider search dialog via #searchMoreIcon.")
        except Exception as e:
            raise RuntimeError(f"Could not open provider search dialog: {e}")

        # Once modal/input is visible, focus the search input
        search_input = wait.until(EC.element_to_be_clickable((By.ID, "searchProviders")))

        # Click the search input to activate it (necessary?)
        try:
            search_input.click()
        except Exception:
            d.execute_script("arguments[0].click();", search_input)

        # Type a shortened query to surface Altafiber
        query = "alta"
        # search_input.clear()
        search_input.send_keys(query)
        logs.append(f"Typed into provider search: {query!r}")

        # wait for the exact text node to appear and be visible
        result = wait.until(EC.visibility_of_element_located(
            (By.XPATH, "//*[normalize-space(.)='altafiber']")
        ))
        # scroll + safe click
        card = result.find_element(By.XPATH, "./ancestor::*[contains(@class,'sc-73b492db')][1]")
        d.execute_script("arguments[0].click();", card)
        try:
            result.click()
        except Exception:

            print('Clicking on card again')
            d.execute_script("arguments[0].click();", result)
        logs.append(f"Selected provider from search results: {provider_name}")
    except Exception as e:
        raise RuntimeError(f"Failed to select provider {provider_name}: {e}")


if __name__ == '__main__':
    auto_login_service = AutoLoginService()
    auto_login_service.authenticate(network='fanduel', code='6URWW9M')

