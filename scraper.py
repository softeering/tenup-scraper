"""Scrape TenUp (FFT) for tennis tournaments around a city.

The TenUp ``/system/ajax`` endpoint is gated by Queue-it bot protection
and requires a ``form_build_id`` token that is rendered into the search
page. Rather than replay that flow with ``requests``, we drive a real
(headless) Chromium instance via Playwright: it naturally clears the
Queue-it challenge, gives us the token, and lets us POST the form from
the same browser context.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

SEARCH_URL = "https://tenup.fft.fr/recherche/tournois"
AJAX_PATH = "/system/ajax"

YOUTH_BUNDLE_AGE_IDS = "70|80|96|97|98|90|95|65|99|100"

REPO_ROOT = Path(__file__).resolve().parent
STORE_PATH = REPO_ROOT / "data" / "tournaments.json"
PAGE_PATH = REPO_ROOT / "docs" / "index.md"

NEW_WINDOW = timedelta(hours=48)
LOCAL_TZ = ZoneInfo("Europe/Paris")

COLLECT_FORM_JS = """
() => {
    const form = document.querySelector('#recherche-tournois-form');
    if (!form) return null;
    const pairs = [];
    for (const el of form.querySelectorAll('input, select, textarea')) {
        if (!el.name) continue;
        if ((el.type === 'checkbox' || el.type === 'radio') && !el.checked) continue;
        if (el.type === 'submit' || el.type === 'button') continue;
        pairs.push([el.name, el.value ?? '']);
    }
    return pairs;
}
"""

FETCH_JS = """
async ({ pairs, overrides }) => {
    const params = new URLSearchParams();
    for (const [k, v] of pairs) params.append(k, v);
    for (const [k, v] of Object.entries(overrides)) {
        params.delete(k);
        params.append(k, v);
    }
    params.append('_triggering_element_name', 'submit_main');
    params.append('_triggering_element_value', 'Rechercher');
    const r = await fetch('%s', {
        method: 'POST',
        headers: {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'x-requested-with': 'XMLHttpRequest',
        },
        body: params.toString(),
    });
    return { status: r.status, body: await r.text() };
}
""" % AJAX_PATH


def fetch_tournaments(
    city: str,
    distance_km: int,
    age_ids: str = YOUTH_BUNDLE_AGE_IDS,
    start: date | None = None,
    end: date | None = None,
    headless: bool = True,
    debug: bool = False,
) -> list[dict]:
    """Query TenUp and return the raw Drupal AJAX command array.

    The returned payload is the list of Drupal AJAX commands the site
    itself would apply to the DOM. Feed it to :func:`parse_tournaments`
    to get a flat list of tournament records.

    Note: the ``categorie_age`` form field is a single checkbox whose
    value is the whole pipe-joined youth bundle. Sending a different
    subset (e.g. ``"65"`` on its own) silently matches no id on the
    server and the query returns zero results. Keep the default, then
    narrow client-side via :func:`parse_tournaments(..., age_id=...)`.

    Args:
        city: City label in the exact format the site expects,
            e.g. ``"Prévessin-Moëns, 01280"``.
        distance_km: Search radius around the city, in kilometres.
        age_ids: Pipe-separated TenUp age-category IDs. Must be a value
            the form actually offers — currently only the full youth
            bundle ``YOUTH_BUNDLE_AGE_IDS``.
        start: Start of the date range (defaults to today).
        end: End of the date range (defaults to 3 months after ``start``).
        headless: Run Chromium headless (default). Set False to debug.
    """
    start = start or date.today()
    end = end or (start + timedelta(days=92))

    overrides = {
        "recherche_type": "ville",
        "ville[autocomplete][country]": "fr",
        "ville[distance][value_field]": str(distance_km),
        "pratique": "TENNIS",
        "date[start]": start.strftime("%d/%m/%y"),
        "date[end]": end.strftime("%d/%m/%y"),
        f"categorie_age[{age_ids}]": age_ids,
        "page": "0",
        "sort": "_DIST_",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_url(f"{SEARCH_URL}**", timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=60_000)

            _dismiss_privacy_overlay(page)
            _select_city(page, city, debug=debug)

            pairs = page.evaluate(COLLECT_FORM_JS)
            if not pairs:
                raise RuntimeError(
                    "Could not locate #recherche-tournois-form on the search page."
                )

            if debug:
                _debug_dump("outgoing form pairs", pairs)
                _debug_dump("overrides", list(overrides.items()))

            result = page.evaluate(
                FETCH_JS, {"pairs": pairs, "overrides": overrides}
            )
        finally:
            browser.close()

    if result["status"] != 200:
        raise RuntimeError(f"TenUp returned HTTP {result['status']}")

    payload = json.loads(result["body"])
    if debug:
        _debug_dump("AJAX response", payload)
    return payload


def _dismiss_privacy_overlay(page) -> None:
    """Hide the TagCommander privacy overlay if it is blocking clicks.

    The consent banner injects ``#privacy-overlay`` which covers the
    whole page and intercepts pointer events. We don't need to "accept"
    anything to submit the search — nuking the overlay is enough and
    leaves cookies at their default state.
    """
    page.evaluate(
        """() => {
            for (const sel of [
                '#privacy-overlay',
                '.tc-privacy-overlay',
                '#tc-privacy-wrapper',
                '.tc-privacy-wrapper',
                '#tc-privacy-center-wrapper',
            ]) {
                for (const el of document.querySelectorAll(sel)) el.remove();
            }
            document.documentElement.style.overflow = '';
            document.body.style.overflow = '';
        }"""
    )


def _select_city(page, city: str, *, debug: bool = False) -> None:
    """Type into the city autocomplete and pick the first suggestion.

    The TenUp form only runs a geo-anchored search when the hidden
    ``value_field`` (and, when available, ``lat_field``/``lng_field``)
    siblings get populated — which only happens when a suggestion is
    selected via the jQuery UI autocomplete's ``select`` event. That
    event is fired reliably by keyboard navigation (ArrowDown → Enter)
    but sometimes not by a plain ``<li>`` click, so we use the keyboard.
    """
    search_term = city.split(",", 1)[0].strip()
    input_sel = "#autocomplete-custom-input"
    input_loc = page.locator(input_sel)
    input_loc.wait_for(state="visible", timeout=10_000)
    input_loc.focus()
    input_loc.fill("")
    input_loc.type(search_term, delay=40)

    suggestion = page.locator("ul.ui-autocomplete li.ui-menu-item").first
    try:
        suggestion.wait_for(state="visible", timeout=10_000)
    except Exception as exc:
        if debug:
            print(
                f"[debug] no autocomplete suggestion for {search_term!r}: {exc}",
                file=sys.stderr,
            )
        raise RuntimeError(
            f"Autocomplete returned no suggestion for city {city!r}. "
            "Try a shorter/exact municipal name."
        )

    input_loc.press("ArrowDown")
    input_loc.press("Enter")

    try:
        page.wait_for_function(
            """() => {
                const v = document.querySelector(
                    'input[name=\"ville[autocomplete][value_container][value_field]\"]'
                );
                return v && v.value && v.value.length > 0;
            }""",
            timeout=10_000,
        )
    except Exception:
        if debug:
            state = page.evaluate(
                """() => {
                    const names = [
                        'ville[autocomplete][textfield]',
                        'ville[autocomplete][value_container][value_field]',
                        'ville[autocomplete][value_container][label_field]',
                        'ville[autocomplete][value_container][lat_field]',
                        'ville[autocomplete][value_container][lng_field]',
                    ];
                    const out = {};
                    for (const n of names) {
                        const el = document.querySelector(`input[name=\"${n}\"]`);
                        out[n] = el ? el.value : null;
                    }
                    return out;
                }"""
            )
            _debug_dump("ville inputs after select", state)
        raise


def _debug_dump(label: str, obj) -> None:
    print(f"[debug] {label}:", file=sys.stderr)
    print(json.dumps(obj, indent=2, ensure_ascii=False), file=sys.stderr)


def parse_tournaments(
    payload: list[dict], age_id: str | None = YOUTH_BUNDLE_AGE_IDS
) -> list[dict]:
    """Extract a flat list of tournament summaries from the AJAX payload.

    Each returned record contains the natural compound key (``code`` —
    millésime + codeClub + zero-padded id — plus ``id`` on its own),
    the tournament name, start/end dates (ISO), and a location block.

    Args:
        payload: The Drupal AJAX command array returned by
            :func:`fetch_tournaments`.
        age_id: Pipe-separated TenUp ``categorieAge.id`` values to keep.
            Tournaments that expose an ``epreuve`` matching any of these
            ids are kept. Defaults to the full youth bundle
            (``YOUTH_BUNDLE_AGE_IDS``). Pass ``None`` to disable filtering.
    """
    allowed_ids: set[int] | None
    if age_id is None:
        allowed_ids = None
    else:
        allowed_ids = {int(x) for x in age_id.split("|") if x.strip()}

    items: list[dict] = []
    for entry in payload:
        if entry.get("command") == "recherche_tournois_update":
            items = entry.get("results", {}).get("items", []) or []
            break
        if (
            entry.get("command") == "insert"
            and entry.get("selector") == "#form-tournois-errors"
            and entry.get("data")
        ):
            raise RuntimeError(
                f"TenUp rejected the search: {entry['data']}"
            )

    out = []
    for item in items:
        if allowed_ids is not None and not _has_any_age(item, allowed_ids):
            continue
        installation = item.get("installation") or {}
        out.append(
            {
                "id": item.get("id"),
                "code": item.get("code"),
                "name": item.get("libelle"),
                "date_start": _iso_date(item.get("dateDebut")),
                "date_end": _iso_date(item.get("dateFin")),
                "location": {
                    "club": item.get("nomClub"),
                    "venue": installation.get("nom"),
                    "address": installation.get("adresse2"),
                    "postal_code": installation.get("codePostal"),
                    "city": installation.get("ville"),
                },
                "distance": item.get("distanceEnMetres"),
            }
        )
    return out


def _has_any_age(item: dict, age_ids: set[int]) -> bool:
    for epreuve in item.get("epreuves") or []:
        cat = (epreuve.get("categorieAge") or {}).get("id")
        if cat in age_ids:
            return True
    return False


def _iso_date(value: dict | str | None) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value[:10]
    raw = value.get("date")
    return raw[:10] if raw else None


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
            "Pipe-separated TenUp categorieAge.id values to keep "
            "(client-side filter). Default is the full youth bundle. "
            "Pass an empty string to disable filtering."
        ),
    )
    parser.add_argument(
        "--headed",
        default=False,
        action="store_true",
        help="Run Chromium in headed mode (for debugging)",
    )
    parser.add_argument(
        "--debug",
        default=False,
        action="store_true",
        help="Dump form pairs and raw AJAX response to stderr",
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
        headless=not args.headed,
        debug=args.debug,
    )
    tournaments = parse_tournaments(
        payload, age_id=args.age_id or None
    )

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
