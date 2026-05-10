"""Indygo Pool API Client.

Uses the official OAuth2 REST API (same as the Android app) instead of
web scraping.  Authentication is done via ``/oauth2/token`` with the
*resource-owner password* grant, and every subsequent call carries a
Bearer token.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import time
from http import HTTPStatus
from typing import Any

import aiohttp

from .const import LOGGER, PROGRAM_TYPE_FILTRATION
from .models import IndygoPoolData
from .parser import IndygoParser

BASE_URL = "https://myindygo.com"

# OAuth2 client credentials extracted from the Android APK (production).
_OAUTH2_CLIENT_ID = "5d1c5bb0b4acd1c748988085"
_OAUTH2_CLIENT_SECRET = "LUowRAajRhZb6NZYqVCFkaLC"
_OAUTH2_BASIC = base64.b64encode(
    f"{_OAUTH2_CLIENT_ID}:{_OAUTH2_CLIENT_SECRET}".encode()
).decode()

# Safety margin before considering the token expired (seconds).
_TOKEN_EXPIRY_MARGIN = 300

# Transient HTTP statuses to retry with exponential backoff.  408 is
# common when the device is asleep on LoRaWAN: the server proxies the
# call to the gateway, the gateway waits for the next radio window, and
# eventually times out.  5xx are generic transient errors.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 502, 503, 504})

# Max retries for transient failures and base backoff (seconds).
_MAX_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_BASE = 2.0


class IndygoPoolApiClientError(Exception):
    """Exception to indicate a general API error."""


class IndygoPoolApiClientAuthenticationError(IndygoPoolApiClientError):
    """Exception to indicate an authentication error."""


class IndygoPoolApiClientCommunicationError(IndygoPoolApiClientError):
    """Exception to indicate a communication error."""


class IndygoPoolApiClient:
    """Indygo Pool API Client."""

    def __init__(
        self,
        email: str,
        password: str,
        pool_id: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize Indygo Pool API Client."""
        self._email = email
        self._password = password
        self._pool_id = pool_id
        self._session = session
        self._parser = IndygoParser()

        # OAuth2 state
        self._token: str | None = None
        self._token_expiry: float = 0

        # Cached hardware identifiers (populated on first data fetch).
        self._pool_address: str | None = None
        self._device_short_id: str | None = None
        self._relay_id: str | None = None

        # Cached rich data
        self._data: IndygoPoolData | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def async_login(self) -> None:
        """Obtain a Bearer token via OAuth2 resource-owner password grant."""
        try:
            async with self._session.post(
                f"{BASE_URL}/oauth2/token",
                headers={"Authorization": f"Basic {_OAUTH2_BASIC}"},
                json={
                    "grant_type": "password",
                    "username": self._email,
                    "password": self._password,
                    "scope": "*",
                },
            ) as resp:
                if resp.status in (
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                ):
                    raise IndygoPoolApiClientAuthenticationError(
                        f"Login failed: {resp.status}"
                    )
                if resp.status != HTTPStatus.OK:
                    text = await resp.text()
                    raise IndygoPoolApiClientCommunicationError(
                        f"Token request failed: {resp.status} - {text}"
                    )
                data = await resp.json()

            if "access_token" not in data:
                raise IndygoPoolApiClientAuthenticationError(
                    f"No access_token in response: {data}"
                )

            self._token = f"{data['token_type']} {data['access_token']}"
            self._token_expiry = time.monotonic() + data.get("expires_in", 3600)
            LOGGER.debug(
                "OAuth2 login successful, token expires in %ss",
                data.get("expires_in"),
            )

        except aiohttp.ClientError as exc:
            raise IndygoPoolApiClientCommunicationError(
                f"Error during login: {exc}"
            ) from exc

    def _token_is_valid(self) -> bool:
        """Return True if the current token is still usable."""
        return (
            self._token is not None
            and time.monotonic() < self._token_expiry - _TOKEN_EXPIRY_MARGIN
        )

    async def _ensure_token(self) -> None:
        """Ensure we have a valid token, refreshing if needed."""
        if not self._token_is_valid():
            await self.async_login()

    async def async_validate_credentials(self) -> None:
        """Validate the credentials and account topology without fetching
        live status from the device.

        Used by the config flow to avoid blocking on a 408 (typical when
        a LoRaWAN device is asleep at the moment of configuration).
        Performs three checks:

        * OAuth2 login succeeds (catches bad email/password).
        * The user has at least one module visible.
        * Hardware identifiers can be resolved (Pool Command + gateway,
          or IPX in fallback).

        Raises ``IndygoPoolApiClientAuthenticationError`` for invalid
        credentials and ``IndygoPoolApiClientError`` for any other
        configuration problem (no modules, no compatible module).
        """
        await self.async_login()

        modules = await self._fetch_modules_metadata()
        if not modules:
            raise IndygoPoolApiClientError(
                "Account has no modules — cannot configure integration."
            )

        # _resolve_hardware_ids will raise IndygoPoolApiClientError if no
        # compatible module is found.
        await self._resolve_hardware_ids(modules)
        LOGGER.debug(
            "Credentials validated: %d modules, pool_address=%s, "
            "device_short_id=%s",
            len(modules),
            self._pool_address,
            self._device_short_id,
        )

    # ------------------------------------------------------------------
    # Generic HTTP helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        data: str | None = None,
        json_body: dict | None = None,
        return_json: bool = False,
        retry_auth: bool = True,
    ) -> dict | str:
        """Perform an authenticated HTTP request.

        Automatically adds the Bearer token, retries once on 401/403 (token
        refresh) and retries up to ``_MAX_TRANSIENT_RETRIES`` times with
        exponential backoff on 408/502/503/504 (typically caused by a
        sleeping LoRaWAN device that did not respond in time).
        """
        await self._ensure_token()

        request_headers: dict[str, str] = {
            "Authorization": self._token,
            "Accept": "version=2.7",
        }
        if headers:
            request_headers.update(headers)

        last_status: int | None = None
        last_text: str | None = None

        for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
            try:
                LOGGER.debug(
                    "--- REQUEST (attempt %d/%d): %s %s ---",
                    attempt + 1,
                    _MAX_TRANSIENT_RETRIES + 1,
                    method,
                    url,
                )
                async with self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    data=data,
                    json=json_body,
                ) as response:
                    if response.status in (
                        HTTPStatus.UNAUTHORIZED,
                        HTTPStatus.FORBIDDEN,
                    ):
                        if retry_auth:
                            LOGGER.debug("Token expired, re-authenticating...")
                            await self.async_login()
                            return await self._request(
                                method,
                                url,
                                headers=headers,
                                data=data,
                                json_body=json_body,
                                return_json=return_json,
                                retry_auth=False,
                            )
                        raise IndygoPoolApiClientAuthenticationError(
                            f"Authentication failed: {response.status}"
                        )

                    if response.status in _RETRYABLE_STATUSES:
                        last_status = response.status
                        try:
                            last_text = await response.text()
                        except Exception:
                            last_text = "<could not read response>"
                        if attempt < _MAX_TRANSIENT_RETRIES:
                            backoff = _TRANSIENT_BACKOFF_BASE * (2**attempt)
                            LOGGER.warning(
                                "Transient %s on %s %s (attempt %d/%d), "
                                "retrying in %.1fs",
                                response.status,
                                method,
                                url,
                                attempt + 1,
                                _MAX_TRANSIENT_RETRIES + 1,
                                backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                        # Out of retries — fall through to error handling
                        LOGGER.error(
                            "Giving up after %d retries on %s %s: %s - %s",
                            _MAX_TRANSIENT_RETRIES,
                            method,
                            url,
                            response.status,
                            last_text,
                        )
                        raise IndygoPoolApiClientCommunicationError(
                            f"Request failed after {_MAX_TRANSIENT_RETRIES} "
                            f"retries: {response.status}"
                        )

                    if response.status != HTTPStatus.OK:
                        try:
                            text = await response.text()
                        except Exception:
                            text = "<could not read response>"
                        LOGGER.error(
                            "API %s %s failed: %s - %s",
                            method,
                            url,
                            response.status,
                            text,
                        )
                        raise IndygoPoolApiClientCommunicationError(
                            f"Request failed: {response.status}"
                        )

                    if return_json:
                        return await response.json()
                    return await response.text()

            except aiohttp.ClientError as exc:
                # Treat connection-level errors as transient too, with the
                # same retry budget.
                if attempt < _MAX_TRANSIENT_RETRIES:
                    backoff = _TRANSIENT_BACKOFF_BASE * (2**attempt)
                    LOGGER.warning(
                        "Network error on %s %s (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        method,
                        url,
                        attempt + 1,
                        _MAX_TRANSIENT_RETRIES + 1,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise IndygoPoolApiClientCommunicationError(
                    f"Error communicating with API: {exc}"
                ) from exc

        # Defensive: should never reach here as the loop always either
        # returns or raises.
        raise IndygoPoolApiClientCommunicationError(
            f"Request failed: status={last_status} body={last_text!r}"
        )

    # ------------------------------------------------------------------
    # API data helpers
    # ------------------------------------------------------------------

    async def _api_call(self, method: str, path: str, body: dict | None = None) -> dict:
        """Perform an API call and return JSON."""
        return await self._request(
            method,
            f"{BASE_URL}{path}",
            json_body=body or {},
            return_json=True,
        )

    async def _api_post(self, path: str, body: dict | None = None) -> dict:
        """POST to ``BASE_URL + path`` and return JSON."""
        return await self._api_call("POST", path, body)

    async def _api_put(self, path: str, body: dict) -> dict:
        """PUT to ``BASE_URL + path`` and return JSON."""
        return await self._api_call("PUT", path, body)

    # ------------------------------------------------------------------
    # Data fetching  (replaces HTML scraping)
    # ------------------------------------------------------------------

    async def _fetch_modules_metadata(self) -> list[dict]:
        """Fetch modules list via /api/getUserWithHisModules."""
        data = await self._api_post("/api/getUserWithHisModules")
        return data.get("modules", [])

    async def _fetch_module_programs(self, module_id: str) -> list[dict]:
        """Fetch programs for a specific module."""
        data = await self._api_post(
            "/api/getModuleWithHisPrograms", {"module": module_id}
        )
        return data.get("programs", [])

    async def _resolve_hardware_ids(self, modules: list[dict]) -> None:
        """Resolve pool_address, device_short_id and relay_id from modules."""
        if self._pool_address and self._device_short_id and self._relay_id:
            return

        pool_address, device_short_id, relay_id = self._parser.resolve_hardware_ids(
            modules
        )

        if not pool_address or not device_short_id or not relay_id:
            raise IndygoPoolApiClientError(
                "Could not determine Pool Address, Device Short ID, or Relay ID "
                f"from {len(modules)} modules."
            )

        self._pool_address = pool_address
        self._device_short_id = device_short_id
        self._relay_id = relay_id

    async def async_get_data(self) -> IndygoPoolData:
        """Get data from the API."""
        # 1. Fetch modules metadata (needed for hardware IDs and programs)
        modules = await self._fetch_modules_metadata()

        # 2. Resolve hardware identifiers once
        await self._resolve_hardware_ids(modules)

        # 3. Enrich modules with their programs (concurrent)
        async def _attach_programs(mod: dict) -> None:
            mod_id = mod.get("id")
            if mod_id:
                programs = await self._fetch_module_programs(str(mod_id))
                if programs:
                    mod["programs"] = programs

        await asyncio.gather(*[_attach_programs(m) for m in modules])

        # 4. Fetch live status data from the device endpoint
        url = (
            f"{BASE_URL}/v1/module/{self._pool_address}/status/{self._device_short_id}"
        )
        LOGGER.debug(
            "Fetching status: pool_address=%s device_short_id=%s relay_id=%s -> %s",
            self._pool_address,
            self._device_short_id,
            self._relay_id,
            url,
        )
        status_data = await self._request(
            "GET",
            url,
            headers={"x-requested-with": "XMLHttpRequest"},
            return_json=True,
        )

        # 5. Merge modules metadata into the status data
        status_data["modules"] = modules

        # 6. Fetch IPX module data if present
        ipx_module = next(
            (m for m in modules if str(m.get("type", "")).startswith("ipx")),
            None,
        )
        if ipx_module:
            status_data["ipx_module"] = ipx_module

        # 7. Parse into structured data
        self._data = self._parser.parse_data(
            status_data,
            self._pool_id,
            self._pool_address,
            self._relay_id,
        )
        return self._data

    # ------------------------------------------------------------------
    # Filtration mode control
    # ------------------------------------------------------------------

    async def async_set_filtration_mode(
        self, module_id: str, full_program_data: dict, mode: int
    ) -> None:
        """Set the filtration mode (Auto/Off/On) safely.

        Sends the FULL program list back (like the Android app) to avoid
        corrupting the device configuration.
        """
        program_copy = copy.deepcopy(full_program_data)

        if "programCharacteristics" not in program_copy:
            raise IndygoPoolApiClientError(
                "Invalid program data: missing programCharacteristics"
            )
        program_copy["programCharacteristics"]["mode"] = mode
        program_copy["dataChanged"] = True

        # Collect all programs for this module
        module_programs = []
        if self._data and module_id in self._data.modules:
            module_programs = self._data.modules[module_id].programs

        # Build the full programs list with updated filtration program
        updated_programs = []
        program_id = program_copy.get("id")
        program_found = False
        for prog in module_programs:
            if prog.get("id") == program_id:
                updated_programs.append(program_copy)
                program_found = True
            else:
                prog_copy = copy.deepcopy(prog)
                prog_copy["dataChanged"] = True
                prog_type = prog_copy.get("programCharacteristics", {}).get(
                    "programType"
                )
                if prog_type != PROGRAM_TYPE_FILTRATION:
                    if (
                        "programCharacteristics" in prog_copy
                        and "mode" in prog_copy["programCharacteristics"]
                    ):
                        prog_copy["programCharacteristics"]["mode"] = None
                updated_programs.append(prog_copy)

        if not program_found:
            updated_programs.append(program_copy)

        LOGGER.debug(
            "Setting filtration mode to %s for module %s. Sending %d programs.",
            mode,
            module_id,
            len(updated_programs),
        )

        try:
            # 1. Update programs in cloud database
            await self._api_put(
                "/api/updatePrograms",
                {"module": module_id, "programs": updated_programs},
            )

            # 2. Push programs to device via cloud→gateway→LoRa relay
            if self._pool_address and self._device_short_id:
                url = (
                    f"/api/module/{self._pool_address}/programs/{self._device_short_id}"
                )
                await self._api_post(url, {"programs": updated_programs})

            # 3. Report data sent
            await self._api_post("/api/reportModuleDatasSent", {"module": module_id})
            await self._api_post(
                "/api/reportProgramsDatasSent",
                {"module": module_id, "programs": updated_programs},
            )

            # 4. LoRaWAN sync for V2 modules
            if (
                self._data
                and module_id in self._data.modules
                and self._data.modules[module_id].raw_data.get("typeIsLoraWanV2", False)
            ):
                await self.async_synchronize_lorawan(
                    module_id, send_program=True, send_command=True
                )

        except IndygoPoolApiClientError as exc:
            LOGGER.error("Failed to set filtration mode: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Remote control  (immediate on/off commands)
    # ------------------------------------------------------------------

    async def async_send_remote_control(
        self,
        mode: str,
        module_serial: str | None = None,
        action: int = 1,
        **kwargs: Any,
    ) -> None:
        """Send an immediate remote control command.

        Args:
            mode: The mode to set ("on", "off", "auto").
            module_serial: Serial number of the module.
            action: Action code (1=Stop, 3=Forced March).
            **kwargs: Additional parameters (e.g. time, manualDuration).
        """
        serial = module_serial or self._pool_address
        if not serial:
            LOGGER.warning("Missing serial number, skipping remote control")
            return

        lines_control_item: dict[str, Any] = {
            "index": 0,
            "mode": mode,
            "action": action,
        }
        if kwargs:
            lines_control_item.update(kwargs)

        payload = {
            "moduleSerialNumber": serial,
            "linesControl": [lines_control_item],
        }

        LOGGER.debug("Sending remote control: %s", payload)
        await self._api_post("/api/setManualCommandToSend", payload)

    async def async_synchronize_lorawan(
        self, module_id: str, send_program: bool = True, send_command: bool = True
    ) -> None:
        """Trigger a LoRaWAN synchronization."""
        payload = {
            "moduleId": module_id,
            "sendProgram": send_program,
            "sendCommand": send_command,
        }
        LOGGER.debug("Triggering LoRaWAN sync: %s", payload)
        try:
            await self._api_post("/modules/sendDataViaLoRaWAN", payload)
        except IndygoPoolApiClientError as exc:
            LOGGER.error("LoRaWAN sync failed: %s", exc)
