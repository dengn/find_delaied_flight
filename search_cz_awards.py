#!/usr/bin/env python3
"""
China Southern (CZ) Award Ticket Availability Searcher

Searches for CZ mileage/award ticket availability by scraping the British Airways
(ba.com) Avios reward flight search page using Playwright.

BA opened reciprocal Avios redemptions with China Southern in July 2025,
allowing CZ economy award flights to be searched and booked on ba.com.

Usage:
    python search_cz_awards.py --origin CAN --dest LAX --date 2026-05-01 --cabin economy
    python search_cz_awards.py --origin CAN --dest LAX --date-range 2026-05-01 2026-05-07 --cabin economy
    python search_cz_awards.py --origin PEK --dest SYD --date 2026-06-15 --cabin business

Requirements:
    pip install playwright
    playwright install chromium
"""

import argparse
import asyncio
import glob
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Optional

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("Error: playwright is required. Install with:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# BA cabin class codes
CABIN_CODES = {
    "economy": "M",
    "premium": "W",
    "business": "C",
    "first": "F",
}

# BA award search base URL
BA_AWARD_SEARCH_URL = "https://www.britishairways.com/travel/redeem/execclub/_gf/en_gb"

# CZ award fare class reference
CZ_AWARD_CLASSES = {
    "F": "First Class Award",
    "O": "Business Class Award",
    "S": "Premium Economy Award",
    "X": "Economy Class Award",
}

# Known CZ hub airports
CZ_HUBS = ["CAN", "PEK", "PKX", "PVG", "WUH", "URC", "SZX", "CTU", "CSX", "DLC"]

# Results cache file
CACHE_FILE = ".cz_award_cache.json"


class CZAwardSearcher:
    """Searches for China Southern award availability via ba.com."""

    def __init__(self, headless: bool = True, slow_mo: int = 0, timeout: int = 60000):
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self.intercepted_api_calls = []
        self.results = []

    @staticmethod
    def _find_chromium_executable() -> Optional[str]:
        """Find an installed Chromium/Chrome executable for Playwright."""
        # Check Playwright cache directories
        pw_cache = os.path.expanduser("~/.cache/ms-playwright")
        if os.path.isdir(pw_cache):
            # Look for chromium installations sorted by version (newest first)
            patterns = [
                os.path.join(pw_cache, "chromium-*/chrome-linux/chrome"),
                os.path.join(pw_cache, "chromium-*/chrome-linux64/chrome"),
                os.path.join(
                    pw_cache,
                    "chromium_headless_shell-*/chrome-linux/headless_shell",
                ),
            ]
            for pattern in patterns:
                matches = sorted(glob.glob(pattern), reverse=True)
                for match in matches:
                    if os.path.isfile(match) and os.access(match, os.X_OK):
                        return match

        # Check system paths
        for cmd in [
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
        ]:
            for path_dir in os.environ.get("PATH", "").split(":"):
                full = os.path.join(path_dir, cmd)
                if os.path.isfile(full) and os.access(full, os.X_OK):
                    return full

        return None

    async def _intercept_response(self, response):
        """Intercept network responses to capture API data."""
        url = response.url
        # Capture potential award search API responses
        if any(
            keyword in url.lower()
            for keyword in [
                "avios",
                "reward",
                "redeem",
                "award",
                "flightsearch",
                "flight-search",
                "offer",
                "availability",
            ]
        ):
            try:
                if "application/json" in (
                    response.headers.get("content-type", "")
                ):
                    body = await response.json()
                    self.intercepted_api_calls.append(
                        {
                            "url": url,
                            "status": response.status,
                            "data": body,
                        }
                    )
                    logger.debug(f"Intercepted API call: {url}")
            except Exception:
                pass

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        cabin: str = "economy",
        adults: int = 1,
    ) -> list[dict]:
        """
        Search for CZ award availability on a single date.

        Args:
            origin: 3-letter IATA airport code (e.g., CAN, PEK)
            destination: 3-letter IATA airport code (e.g., LAX, LHR)
            date: Travel date in YYYY-MM-DD format
            cabin: Cabin class (economy, premium, business, first)
            adults: Number of adult passengers

        Returns:
            List of available award flights with details
        """
        cabin_code = CABIN_CODES.get(cabin.lower(), "M")
        travel_date = datetime.strptime(date, "%Y-%m-%d")

        logger.info(
            f"Searching CZ awards: {origin} -> {destination} on {date} ({cabin})"
        )

        async with async_playwright() as p:
            launch_opts = {
                "headless": self.headless,
                "slow_mo": self.slow_mo,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            }

            # Auto-detect chromium executable if default launch fails
            chromium_path = self._find_chromium_executable()
            if chromium_path:
                launch_opts["executable_path"] = chromium_path
                logger.debug(f"Using chromium at: {chromium_path}")

            browser = await p.chromium.launch(**launch_opts)

            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )

            page = await context.new_page()

            # Intercept network responses to capture API data
            page.on("response", self._intercept_response)

            flights = []

            try:
                flights = await self._search_via_ba(
                    page, origin, destination, travel_date, cabin_code, adults
                )
            except PlaywrightTimeout:
                logger.warning(
                    "BA search timed out. The page may have changed or be blocking."
                )
            except Exception as e:
                logger.error(f"Search failed: {e}")
            finally:
                await browser.close()

            # Filter for CZ-operated flights
            cz_flights = [f for f in flights if f.get("operating_carrier") == "CZ"]
            if not cz_flights and flights:
                logger.info(
                    f"Found {len(flights)} total flights, but none operated by CZ. "
                    "Showing all results."
                )
                cz_flights = flights

            self.results.extend(cz_flights)
            return cz_flights

    async def _search_via_ba(
        self, page, origin, destination, travel_date, cabin_code, adults
    ):
        """Perform the BA.com award search."""
        # Build the BA award search URL with query parameters
        date_str = travel_date.strftime("%d/%m/%Y")
        month_str = travel_date.strftime("%Y%m")

        params = {
            "eId": "111001",
            "tab_selected": "red498",
            "departurePoint": origin.upper(),
            "arrivalPoint": destination.upper(),
            "departDate": date_str,
            "returnDate": "",
            "CabinCode": cabin_code,
            "NumberOfAdults": str(adults),
            "NumberOfYoungAdults": "0",
            "NumberOfChildren": "0",
            "NumberOfInfants": "0",
            "outboundMultiStop": "false",
            "inboundMultiStop": "false",
        }

        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        search_url = f"{BA_AWARD_SEARCH_URL}?{query_string}"

        logger.info(f"Navigating to BA award search...")
        logger.debug(f"URL: {search_url}")

        # Navigate to the search page
        await page.goto(search_url, wait_until="networkidle", timeout=self.timeout)

        # Handle cookie consent banner if present
        try:
            accept_btn = page.locator(
                "button:has-text('Accept all cookies'), "
                "button:has-text('Accept All'), "
                "button:has-text('OK'), "
                "#onetrust-accept-btn-handler"
            )
            if await accept_btn.count() > 0:
                await accept_btn.first.click()
                logger.debug("Accepted cookies")
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Wait for results to load
        logger.info("Waiting for search results...")
        await page.wait_for_timeout(5000)

        # Try to detect if we need to log in
        login_indicators = [
            "text=Sign in",
            "text=Log in",
            "text=Enter your details",
            "#membershipNumber",
        ]
        for indicator in login_indicators:
            try:
                if await page.locator(indicator).count() > 0:
                    logger.warning(
                        "BA requires login to search award flights. "
                        "Please set BA_USERNAME and BA_PASSWORD environment variables, "
                        "or run with --no-headless to log in manually."
                    )
                    # Attempt auto-login if credentials available
                    username = os.environ.get("BA_USERNAME", "")
                    password = os.environ.get("BA_PASSWORD", "")
                    if username and password:
                        await self._ba_login(page, username, password)
                    else:
                        if not self.headless:
                            logger.info(
                                "Please log in manually in the browser window. "
                                "Waiting 120 seconds..."
                            )
                            await page.wait_for_timeout(120000)
                    break
            except Exception:
                continue

        # Wait for flight results
        await page.wait_for_timeout(5000)

        # Parse the results page
        flights = await self._parse_ba_results(page, travel_date)

        # Also check intercepted API data
        api_flights = self._parse_intercepted_data()
        if api_flights:
            logger.info(
                f"Found {len(api_flights)} flights from intercepted API data"
            )
            flights.extend(api_flights)

        # Deduplicate by flight number + date
        seen = set()
        unique_flights = []
        for f in flights:
            key = f"{f.get('flight_number', '')}-{f.get('departure_time', '')}"
            if key not in seen:
                seen.add(key)
                unique_flights.append(f)

        return unique_flights

    async def _ba_login(self, page, username, password):
        """Attempt to log in to BA Executive Club."""
        logger.info("Attempting BA login...")
        try:
            # Fill membership number
            member_input = page.locator(
                "#membershipNumber, "
                "input[name='membershipNumber'], "
                "input[placeholder*='membership'], "
                "input[placeholder*='Membership']"
            )
            if await member_input.count() > 0:
                await member_input.first.fill(username)

            # Fill password/PIN
            pass_input = page.locator(
                "#input_password, "
                "input[name='password'], "
                "input[type='password']"
            )
            if await pass_input.count() > 0:
                await pass_input.first.fill(password)

            # Click sign in
            signin_btn = page.locator(
                "button:has-text('Sign in'), "
                "button:has-text('Log in'), "
                "input[type='submit']"
            )
            if await signin_btn.count() > 0:
                await signin_btn.first.click()
                await page.wait_for_timeout(5000)
                logger.info("Login submitted")
        except Exception as e:
            logger.warning(f"Auto-login failed: {e}")

    async def _parse_ba_results(self, page, travel_date) -> list[dict]:
        """Parse flight results from the BA results page."""
        flights = []

        # Take a screenshot for debugging
        screenshot_path = f"ba_search_{travel_date.strftime('%Y%m%d')}.png"
        try:
            await page.screenshot(path=screenshot_path, full_page=True)
            logger.debug(f"Screenshot saved: {screenshot_path}")
        except Exception:
            pass

        # Get the page content for analysis
        content = await page.content()

        # Check for "no flights" messages
        no_flights_patterns = [
            "no available flights",
            "no flights found",
            "no reward flights",
            "sorry, there are no",
            "no Avios flights",
            "we couldn't find",
            "no results",
        ]
        content_lower = content.lower()
        for pattern in no_flights_patterns:
            if pattern in content_lower:
                logger.info(f"No award flights available for {travel_date.strftime('%Y-%m-%d')}")
                return []

        # Try to parse structured flight result elements
        # BA uses various class names for flight cards
        flight_selectors = [
            ".flight-result",
            ".flight-row",
            ".flight-card",
            "[data-test='flight-card']",
            ".flightListItem",
            ".journey-option",
            "tr.flight",
            ".outbound-flight",
            ".result-row",
        ]

        for selector in flight_selectors:
            try:
                elements = await page.locator(selector).all()
                if elements:
                    logger.info(
                        f"Found {len(elements)} flight elements with selector: {selector}"
                    )
                    for el in elements:
                        flight = await self._parse_flight_element(el, travel_date)
                        if flight:
                            flights.append(flight)
                    break
            except Exception:
                continue

        # Fallback: extract text-based flight info from the page
        if not flights:
            flights = await self._parse_flights_from_text(page, travel_date)

        return flights

    async def _parse_flight_element(self, element, travel_date) -> Optional[dict]:
        """Parse a single flight result element."""
        try:
            text = await element.inner_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            flight = {
                "date": travel_date.strftime("%Y-%m-%d"),
                "raw_text": " | ".join(lines[:10]),
                "operating_carrier": "",
                "flight_number": "",
                "departure_time": "",
                "arrival_time": "",
                "origin": "",
                "destination": "",
                "avios_cost": "",
                "cabin": "",
                "stops": "",
                "duration": "",
            }

            for line in lines:
                # Extract flight number (CZ followed by digits)
                cz_match = re.search(r"\b(CZ\s*\d{3,4})\b", line)
                if cz_match:
                    flight["flight_number"] = cz_match.group(1).replace(" ", "")
                    flight["operating_carrier"] = "CZ"

                # Extract Avios cost
                avios_match = re.search(
                    r"([\d,]+)\s*Avios", line, re.IGNORECASE
                )
                if avios_match:
                    flight["avios_cost"] = avios_match.group(1)

                # Extract times (HH:MM format)
                time_match = re.findall(r"\b(\d{1,2}:\d{2})\b", line)
                if time_match and not flight["departure_time"]:
                    flight["departure_time"] = time_match[0]
                    if len(time_match) > 1:
                        flight["arrival_time"] = time_match[1]

                # Extract duration
                dur_match = re.search(r"(\d+h\s*\d*m?)", line, re.IGNORECASE)
                if dur_match:
                    flight["duration"] = dur_match.group(1)

                # Extract stops
                if "direct" in line.lower() or "non-stop" in line.lower():
                    flight["stops"] = "Direct"
                stop_match = re.search(r"(\d+)\s*stop", line, re.IGNORECASE)
                if stop_match:
                    flight["stops"] = f"{stop_match.group(1)} stop(s)"

            return flight if flight["flight_number"] or flight["avios_cost"] else None
        except Exception:
            return None

    async def _parse_flights_from_text(self, page, travel_date) -> list[dict]:
        """Fallback: parse flights from visible page text."""
        flights = []
        try:
            text = await page.inner_text("body")
            # Look for CZ flight numbers anywhere on the page
            cz_matches = re.findall(r"CZ\s*\d{3,4}", text)
            if cz_matches:
                logger.info(
                    f"Found CZ flight references in page text: {cz_matches}"
                )
                for match in cz_matches:
                    flight_num = match.replace(" ", "")
                    flights.append(
                        {
                            "date": travel_date.strftime("%Y-%m-%d"),
                            "flight_number": flight_num,
                            "operating_carrier": "CZ",
                            "raw_text": f"CZ flight found: {flight_num}",
                            "departure_time": "",
                            "arrival_time": "",
                            "origin": "",
                            "destination": "",
                            "avios_cost": "",
                            "cabin": "",
                            "stops": "",
                            "duration": "",
                        }
                    )

            # Look for Avios costs
            avios_matches = re.findall(
                r"([\d,]+)\s*Avios", text, re.IGNORECASE
            )
            if avios_matches:
                logger.info(f"Found Avios costs: {avios_matches}")

        except Exception as e:
            logger.debug(f"Text parsing failed: {e}")

        return flights

    def _parse_intercepted_data(self) -> list[dict]:
        """Parse flights from intercepted API responses."""
        flights = []
        for call in self.intercepted_api_calls:
            data = call.get("data", {})
            try:
                # Try to extract flight data from various API response formats
                if isinstance(data, dict):
                    # Look for flight offers/results in common response structures
                    for key in [
                        "flights",
                        "offers",
                        "results",
                        "journeys",
                        "outbound",
                        "flightResults",
                        "data",
                    ]:
                        if key in data and isinstance(data[key], list):
                            for item in data[key]:
                                flight = self._extract_flight_from_api(item)
                                if flight:
                                    flights.append(flight)
            except Exception:
                continue

        return flights

    def _extract_flight_from_api(self, item) -> Optional[dict]:
        """Extract flight info from an API response item."""
        if not isinstance(item, dict):
            return None

        flight = {
            "date": "",
            "flight_number": "",
            "operating_carrier": "",
            "departure_time": "",
            "arrival_time": "",
            "origin": "",
            "destination": "",
            "avios_cost": "",
            "cabin": "",
            "stops": "",
            "duration": "",
            "raw_text": "",
            "source": "api",
        }

        # Try common field names
        for fn_key in ["flightNumber", "flight_number", "number", "designator"]:
            if fn_key in item:
                val = str(item[fn_key])
                if "CZ" in val.upper():
                    flight["flight_number"] = val
                    flight["operating_carrier"] = "CZ"

        for dep_key in [
            "departureTime",
            "departure_time",
            "departTime",
            "departure",
        ]:
            if dep_key in item:
                flight["departure_time"] = str(item[dep_key])

        for arr_key in ["arrivalTime", "arrival_time", "arriveTime", "arrival"]:
            if arr_key in item:
                flight["arrival_time"] = str(item[arr_key])

        for cost_key in [
            "avios",
            "aviosCost",
            "avios_cost",
            "points",
            "miles",
            "price",
        ]:
            if cost_key in item:
                flight["avios_cost"] = str(item[cost_key])

        for orig_key in ["origin", "departureAirport", "from"]:
            if orig_key in item:
                flight["origin"] = str(item[orig_key])

        for dest_key in ["destination", "arrivalAirport", "to"]:
            if dest_key in item:
                flight["destination"] = str(item[dest_key])

        # Look in nested segments
        segments = item.get("segments", item.get("legs", []))
        if isinstance(segments, list):
            for seg in segments:
                if isinstance(seg, dict):
                    carrier = seg.get(
                        "operatingCarrier",
                        seg.get("carrier", seg.get("airline", "")),
                    )
                    if "CZ" in str(carrier).upper():
                        flight["operating_carrier"] = "CZ"
                        flight["flight_number"] = seg.get(
                            "flightNumber",
                            seg.get("number", flight["flight_number"]),
                        )

        if flight["operating_carrier"] == "CZ" or flight["avios_cost"]:
            flight["raw_text"] = json.dumps(item, default=str)[:500]
            return flight

        return None

    async def search_date_range(
        self,
        origin: str,
        destination: str,
        start_date: str,
        end_date: str,
        cabin: str = "economy",
        adults: int = 1,
    ) -> list[dict]:
        """
        Search for CZ award availability across a date range.

        Args:
            origin: 3-letter IATA airport code
            destination: 3-letter IATA airport code
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            cabin: Cabin class
            adults: Number of adult passengers

        Returns:
            List of available award flights across all dates
        """
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        all_flights = []

        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            try:
                flights = await self.search(
                    origin, destination, date_str, cabin, adults
                )
                all_flights.extend(flights)
                logger.info(
                    f"Date {date_str}: {len(flights)} CZ award flight(s) found"
                )
            except Exception as e:
                logger.error(f"Failed to search {date_str}: {e}")

            # Rate limiting - wait between searches to avoid detection
            await asyncio.sleep(3)
            current += timedelta(days=1)

        return all_flights

    def save_results(self, filepath: str = "cz_award_results.json"):
        """Save search results to a JSON file."""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "search_time": datetime.now().isoformat(),
                    "results_count": len(self.results),
                    "results": self.results,
                    "intercepted_api_calls": [
                        {
                            "url": c["url"],
                            "status": c["status"],
                        }
                        for c in self.intercepted_api_calls
                    ],
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        logger.info(f"Results saved to {filepath}")

    def print_results(self):
        """Print search results in a formatted table."""
        if not self.results:
            print("\nNo CZ award flights found.")
            print("\nTips:")
            print("  - CZ economy awards (X class) are most commonly available")
            print("  - Business class (O class) rarely appears on ba.com yet")
            print("  - Try different dates or routes")
            print(
                "  - Validate availability with 航旅纵横 (Umetrip) to avoid ghost tickets"
            )
            return

        print(f"\n{'='*80}")
        print(f"  CZ Award Flights Found: {len(self.results)}")
        print(f"{'='*80}")

        for i, flight in enumerate(self.results, 1):
            print(f"\n  [{i}] {flight.get('flight_number', 'N/A')}")
            print(f"      Date:      {flight.get('date', 'N/A')}")
            print(
                f"      Route:     {flight.get('origin', '?')} -> {flight.get('destination', '?')}"
            )
            print(
                f"      Time:      {flight.get('departure_time', '?')} - {flight.get('arrival_time', '?')}"
            )
            print(f"      Duration:  {flight.get('duration', 'N/A')}")
            print(f"      Cabin:     {flight.get('cabin', 'N/A')}")
            print(f"      Avios:     {flight.get('avios_cost', 'N/A')}")
            print(f"      Stops:     {flight.get('stops', 'N/A')}")
            if flight.get("operating_carrier") == "CZ":
                print(f"      Carrier:   China Southern (CZ)")

        print(f"\n{'='*80}")
        print(
            "Note: Validate results with 航旅纵横 (Umetrip) to confirm real availability."
        )
        print(
            "      Check O class (business) or X class (economy) seat counts."
        )
        print(f"{'='*80}\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Search for China Southern (CZ) award ticket availability via ba.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --origin CAN --dest LAX --date 2026-05-01
  %(prog)s --origin CAN --dest LAX --date-range 2026-05-01 2026-05-07
  %(prog)s --origin PEK --dest SYD --date 2026-06-15 --cabin business
  %(prog)s --origin CAN --dest LHR --date 2026-04-01 --no-headless

Environment variables:
  BA_USERNAME    BA Executive Club membership number (for auto-login)
  BA_PASSWORD    BA Executive Club password/PIN (for auto-login)

CZ Award Fare Classes:
  F = First Class    O = Business Class
  S = Premium Econ   X = Economy Class
        """,
    )

    parser.add_argument(
        "--origin",
        required=True,
        help="Departure airport IATA code (e.g., CAN, PEK, PVG)",
    )
    parser.add_argument(
        "--dest",
        required=True,
        help="Arrival airport IATA code (e.g., LAX, LHR, SYD)",
    )
    parser.add_argument(
        "--date",
        help="Travel date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--date-range",
        nargs=2,
        metavar=("START", "END"),
        help="Date range in YYYY-MM-DD YYYY-MM-DD format",
    )
    parser.add_argument(
        "--cabin",
        choices=["economy", "premium", "business", "first"],
        default="economy",
        help="Cabin class (default: economy)",
    )
    parser.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adult passengers (default: 1)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful for manual login or debugging)",
    )
    parser.add_argument(
        "--output",
        default="cz_award_results.json",
        help="Output JSON file path (default: cz_award_results.json)",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Slow down browser actions by milliseconds (for debugging)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60000,
        help="Page load timeout in milliseconds (default: 60000)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if not args.date and not args.date_range:
        parser.error("Either --date or --date-range is required")

    return args


async def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    searcher = CZAwardSearcher(
        headless=not args.no_headless,
        slow_mo=args.slow_mo,
        timeout=args.timeout,
    )

    if args.date:
        await searcher.search(
            origin=args.origin,
            destination=args.dest,
            date=args.date,
            cabin=args.cabin,
            adults=args.adults,
        )
    elif args.date_range:
        await searcher.search_date_range(
            origin=args.origin,
            destination=args.dest,
            start_date=args.date_range[0],
            end_date=args.date_range[1],
            cabin=args.cabin,
            adults=args.adults,
        )

    searcher.print_results()
    searcher.save_results(args.output)


if __name__ == "__main__":
    asyncio.run(main())
