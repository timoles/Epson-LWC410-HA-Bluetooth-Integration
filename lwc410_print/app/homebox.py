"""Minimal Homebox API client: look up an entity's name/location for a label.

Read-only, authenticated with a Homebox API key (Profile -> API keys; sent as
"Authorization: Bearer <key>"). Endpoints verified against Homebox v0.27.1,
where items and locations are both "entities":
    GET /api/v1/assets/{asset id}  -> {"items": [EntitySummary...]}  (parent = location)
    GET /api/v1/entities/{uuid}    -> EntityOut (name, assetId, location, parent)
Asset ids are accepted as 42, 000042 or 000-042.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import render

TIMEOUT = 10


class HomeboxError(ValueError):
    pass


class HomeboxClient:
    def __init__(self, base_url, api_key):
        self.base = (base_url or "").rstrip("/")
        if not self.base.lower().startswith(("http://", "https://")):
            raise HomeboxError(f"invalid Homebox API URL {base_url!r}")
        key = (api_key or "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise HomeboxError("Homebox API key is empty")
        self.auth = "Bearer " + key

    def get(self, path):
        req = urllib.request.Request(
            self.base + "/api" + path,
            headers={"Accept": "application/json", "Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise HomeboxError("Homebox rejected the API key (401)")
            if exc.code == 404:
                raise HomeboxError("not found in Homebox")
            raise HomeboxError(f"Homebox API error {exc.code} for {path}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HomeboxError(f"cannot reach Homebox at {self.base}: {exc}")

    def lookup(self, asset_id=None, item_id=None):
        """-> {"name", "location", "asset_id"} (values may be None)."""
        if asset_id:
            aid = render.format_asset_id(asset_id)
            data = self.get("/v1/assets/" + urllib.parse.quote(aid))
            items = data.get("items") or []
            if not items:
                raise HomeboxError(f"no Homebox entry with asset id {aid}")
            return self._summary(items[0], aid)
        if not render.UUID_RE.match(str(item_id or "")):
            raise HomeboxError(f"invalid Homebox item id {item_id!r}")
        return self._summary(self.get("/v1/entities/" + str(item_id).lower()), None)

    @staticmethod
    def _summary(entity, asset_id):
        location = None
        for key in ("location", "parent"):
            ref = entity.get(key)
            if isinstance(ref, dict) and ref.get("name"):
                location = ref["name"]
                break
        aid = asset_id
        raw = entity.get("assetId")
        if aid is None and raw not in (None, "", 0, "0", "000-000"):
            try:
                aid = render.format_asset_id(raw)
            except ValueError:
                aid = None
        return {"name": entity.get("name"), "location": location, "asset_id": aid}
