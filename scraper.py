"""Scrape TenUp (FFT) for tennis tournaments around a city.

TenUp was rebuilt as a Nuxt/Vue single-page app. It talks to a public
JSON backend that is *not* behind Queue-it bot protection, so a plain
``requests`` session is enough — no browser automation required.

Two endpoints are involved:

``GET /back/public/v1/autocompletion/villes?recherche=<name>``
    Geocodes a city name to a ``{ville, codePostal, latitude,
    longitude, ...}`` record (same data the site's autocomplete box
    shows).

``POST /back/public/v1/tournois``
    Searches tournaments around a ``lat``/``lng`` point within a
    ``distance`` (km), date range, and (optionally) a list of
    ``categoriesAge`` ids. Returns ``{nbResultats, cards: [...]}``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

AUTOCOMPLETE_URL = "https://tenup.fft.fr/back/public/v1/autocompletion/villes"
SEARCH_URL = "https://tenup.fft.fr/back/public/v1/tournois"

YOUTH_BUNDLE_AGE_IDS = "70|80|96|97|98|90|95|65|99|100"

REPO_ROOT = Path(__file__).resolve().parent
STORE_PATH = REPO_ROOT / "data" / "tournaments.json"
PAGE_PATH = REPO_ROOT / "docs" / "index.md"

NEW_WINDOW = timedelta(hours=48)
LOCAL_TZ = ZoneInfo("Europe/Paris")

REQUEST_TIMEOUT = 30


def geocode_city(city: str, *, session: requests.Session | None = None) -> dict:
    """Resolve a city label to TenUp's autocomplete record (lat/lng included).

    Args:
        city: City label, e.g. ``"Prévessin-Moëns, 01280"``. The part
            before the first comma is used as the search term; if a
            postal code follows, it is used to disambiguate between
            several same-named towns.
    """
    session = session or requests.Session()
    name, _, postal_code = city.partition(",")
    name = name.strip()
    postal_code = postal_code.strip()

    resp = session.get(
        AUTOCOMPLETE_URL,
        params={"recherche": name},
        headers={"accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    candidates = resp.json()
    if not candidates:
        raise RuntimeError(
            f"Autocomplete returned no suggestion for city {city!r}. "
            "Try a shorter/exact municipal name."
        )

    if postal_code:
        for candidate in candidates:
            if candidate.get("codePostal") == postal_code:
                return candidate

    return candidates[0]


def fetch_tournaments(
    city: str,
    distance_km: int,
    age_ids: str = YOUTH_BUNDLE_AGE_IDS,
    start: date | None = None,
    end: date | None = None,
    debug: bool = False,
    **_ignored,
) -> dict:
    """Query the TenUp tournament-search API and return the raw JSON payload.

    Feed the result to :func:`parse_tournaments` to get a flat list of
    tournament records.

    Args:
        city: City label in the exact format the site expects,
            e.g. ``"Prévessin-Moëns, 01280"``.
        distance_km: Search radius around the city, in kilometres.
        age_ids: Pipe-separated TenUp ``categorieAge.id`` values to
            filter on server-side. Pass ``None``/``""`` to disable
            filtering (search every age category).
        start: Start of the date range (defaults to today).
        end: End of the date range (defaults to 3 months after ``start``).
    """
    start = start or date.today()
    end = end or (start + timedelta(days=92))

    session = requests.Session()
    location = geocode_city(city, session=session)
    if debug:
        _debug_dump("geocoded city", location)

    age_id_list = [int(x) for x in age_ids.split("|") if x.strip()] if age_ids else []

    body = {
        "pratique": "TENNIS",
        "from": 0,
        "size": 1000,
        "lat": location["latitude"],
        "lng": location["longitude"],
        "distance": distance_km,
        "type": [],
        "codeClub": None,
        "ligues": [],
        "comites": [],
        "dateDebut": datetime.combine(start, datetime.min.time()).isoformat() + "Z",
        "dateFin": datetime.combine(end, datetime.min.time()).isoformat() + "Z",
        "utiliserMesDonnees": False,
        "naturesEpreuves": [],
        "typesEpreuves": [],
        "naturesTerrains": [],
        "categoriesJeu": [],
        "categoriesAge": age_id_list,
        "familles": [],
        "tournoiInterne": False,
        "classements": [],
        "inscriptionEnLigne": None,
        "paiementEnLigne": None,
        "filtres": True,
        "sort": "DISTANCE",
    }

    if debug:
        _debug_dump("outgoing search body", body)

    resp = session.post(
        SEARCH_URL,
        json=body,
        headers={"accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"TenUp returned HTTP {resp.status_code}: {resp.text[:500]}")

    payload = resp.json()
    if debug:
        _debug_dump("search response", payload)
    return payload


def _debug_dump(label: str, obj) -> None:
    print(f"[debug] {label}:", file=sys.stderr)
    print(json.dumps(obj, indent=2, ensure_ascii=False), file=sys.stderr)


def parse_tournaments(payload: dict, age_id: str | None = None) -> list[dict]:
    """Extract a flat list of tournament summaries from the search response.

    Each returned record uses the tournament's ``idHomologation`` (e.g.
    ``"MOJA_208847"``) as its natural unique key, the tournament name,
    start/end dates (ISO), and a location block.

    Args:
        payload: The JSON body returned by :func:`fetch_tournaments`
            (``{"nbResultats": int, "cards": [...]}``).
        age_id: Unused — kept for backward-compatible call sites. Age
            filtering now happens server-side via the ``categoriesAge``
            field in the search request (see :func:`fetch_tournaments`).
    """
    cards = payload.get("cards") or []

    out = []
    for card in cards:
        club = card.get("club") or {}
        out.append(
            {
                "id": card.get("idHomologation"),
                "name": card.get("libelleTournoi"),
                "date_start": _iso_date(card.get("dateDebut")),
                "date_end": _iso_date(card.get("dateFin")),
                "location": {
                    "club": club.get("libelle"),
                    "city": card.get("ville"),
                },
                "distance": _format_distance(card.get("distance")),
            }
        )
    return out


def _iso_date(value: dict | str | None) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value[:10]
    raw = value.get("date")
    return raw[:10] if raw else None


def _format_distance(meters: float | int | None) -> str | None:
    """Format a distance in meters as a French-style km string (``"43,1 km"``)."""
    if meters is None:
        return None
    km = round(meters / 1000, 1)
    return f"{km:.1f}".replace(".", ",") + " km"


def load_store(path: Path = STORE_PATH) -> dict:
    if not path.exists():
        return {"metadata": {}, "tournaments": []}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_store(store: dict, path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def refresh_store(
    new_tournaments: list[dict],
    store: dict,
    *,
    now: datetime | None = None,
    today: date | None = None,
    scrape_params: dict | None = None,
) -> dict:
    """Merge freshly-scraped tournaments into ``store`` in place.

    Upserts by ``id``, drops entries whose ``date_start`` is on or before
    ``today`` (defaults to today in ``LOCAL_TZ``), sorts by ``date_start``
    asc, and stamps ``metadata`` with scrape time + parameters.
    """
    now = now or datetime.now(LOCAL_TZ)
    today = today or datetime.now(LOCAL_TZ).date()
    now_iso = now.replace(microsecond=0).isoformat()

    existing: dict[str, dict] = {}
    for item in store.get("tournaments") or []:
        if item.get("id"):
            existing[item["id"]] = item

    merged: dict[str, dict] = dict(existing)
    for item in new_tournaments:
        tid = item.get("id")
        if not tid:
            continue
        prev = existing.get(tid)
        first_seen = prev.get("first_seen") if prev else None
        merged[tid] = {**item, "first_seen": first_seen or now_iso}

    kept = [t for t in merged.values() if _is_future(t, today)]
    kept.sort(
        key=lambda t: (t.get("date_start") or "9999-12-31", t.get("id") or ""),
    )

    metadata = dict(store.get("metadata") or {})
    metadata["last_scrape"] = now_iso
    metadata["tournament_count"] = len(kept)
    if scrape_params:
        metadata["scrape_params"] = scrape_params

    store["metadata"] = metadata
    store["tournaments"] = kept
    return store


def _is_future(tournament: dict, today: date) -> bool:
    raw = tournament.get("date_start")
    if not raw:
        return True
    try:
        return date.fromisoformat(raw[:10]) >= today
    except ValueError:
        return True


def render_markdown(store: dict, *, now: datetime | None = None) -> str:
    """Render the datastore as a GitHub-Pages-friendly markdown page."""
    meta = store.get("metadata") or {}
    tournaments = store.get("tournaments") or []
    params = meta.get("scrape_params") or {}
    now = now or datetime.now(LOCAL_TZ)

    lines = [
        "---",
        "title: Tournois TenUp",
        "---",
        "",
        f"# Tournois de tennis à venir ({len(tournaments)})",
        "",
    ]

    last_scrape = meta.get("last_scrape")
    if last_scrape:
        lines.append(f"_Dernière mise à jour : {last_scrape}_  ")
    if params:
        bits = []
        if params.get("city"):
            bits.append(f"ville **{params['city']}**")
        if params.get("distance_km") is not None:
            bits.append(f"rayon **{params['distance_km']} km**")
        if params.get("age_id") and params["age_id"] != YOUTH_BUNDLE_AGE_IDS:
            bits.append(f"catégorie d'âge **{params['age_id']}**")
        if bits:
            lines.append("_Recherche : " + ", ".join(bits) + "._")
    lines.append("")

    if not tournaments:
        lines.append("Aucun tournoi à afficher pour le moment.")
        return "\n".join(lines) + "\n"

    lines.append('<table class="tournaments">')
    lines.append(
        "  <thead><tr>"
        "<th>Date</th><th>Tournoi</th><th>Club</th>"
        "<th>Ville</th><th>Distance</th>"
        "</tr></thead>"
    )
    lines.append("  <tbody>")
    for t in tournaments:
        row_class = ' class="is-new"' if _is_new(t, now) else ""
        lines.append(
            "    <tr{cls}>"
            "<td>{date}</td>"
            "<td>{name}{badge}</td>"
            "<td>{club}</td>"
            "<td>{city}</td>"
            "<td>{distance}</td>"
            "</tr>".format(
                cls=row_class,
                date=_html_escape(_format_date_range(t)),
                name=_format_name_html(t),
                badge=' <span class="new-badge">NEW</span>' if _is_new(t, now) else "",
                club=_html_escape((t.get("location") or {}).get("club") or ""),
                city=_html_escape((t.get("location") or {}).get("city") or ""),
                distance=_html_escape(t.get("distance") or ""),
            )
        )
    lines.append("  </tbody>")
    lines.append("</table>")

    return "\n".join(lines) + "\n"


def _is_new(t: dict, now: datetime) -> bool:
    raw = t.get("first_seen")
    if not raw:
        return False
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=LOCAL_TZ)
    return (now - seen) <= NEW_WINDOW


def _format_name_html(t: dict) -> str:
    name = _html_escape(t.get("name") or "")
    tid = t.get("id")
    if not name:
        return ""
    if not tid:
        return name
    return (
        f'<a href="https://tenup.fft.fr/tournoi/{tid}" '
        f'target="_blank" rel="noopener noreferrer">{name}</a>'
    )


def _format_date_range(t: dict) -> str:
    start = t.get("date_start") or ""
    end = t.get("date_end") or ""
    if start and end and start != end:
        return f"{start} → {end}"
    return start or end or ""


def _html_escape(value: str | None) -> str:
    if not value:
        return ""
    s = str(value).strip()
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_page(store: dict, path: Path = PAGE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(store), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--city",
        default="Prévessin-Moëns, 01280",
        help='City label, e.g. "Prévessin-Moëns, 01280"',
    )
    parser.add_argument(
        "--distance",
        type=int,
        default=50,
        help="Search radius in kilometres (default: 50)",
    )
    parser.add_argument(
        "--age-id",
        type=str,
        default=YOUTH_BUNDLE_AGE_IDS,
        help=(
            "Pipe-separated TenUp categorieAge.id values to filter on "
            "(applied server-side in the search request). Default is "
            "the full youth bundle. Pass an empty string to disable "
            "filtering (search every age category)."
        ),
    )
    parser.add_argument(
        "--debug",
        default=False,
        action="store_true",
        help="Dump the geocoded city, outgoing search body, and raw response to stderr",
    )
    parser.add_argument(
        "--print-only",
        default=False,
        action="store_true",
        help="Scrape and print JSON to stdout without touching the store or page",
    )
    args = parser.parse_args()

    payload = fetch_tournaments(
        args.city,
        args.distance,
        age_ids=args.age_id or None,
        debug=args.debug,
    )
    tournaments = parse_tournaments(payload)

    if args.print_only:
        print(json.dumps(tournaments, indent=2, ensure_ascii=False))
        return

    store = load_store()
    refresh_store(
        tournaments,
        store,
        scrape_params={
            "city": args.city,
            "distance_km": args.distance,
            "age_id": args.age_id or None,
        },
    )
    save_store(store)
    write_page(store)

    print(
        f"Stored {len(store['tournaments'])} tournament(s) → {STORE_PATH}",
        file=sys.stderr,
    )
    print(f"Rendered page → {PAGE_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
