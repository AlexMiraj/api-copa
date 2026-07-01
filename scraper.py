"""
Scraper para a página de agenda da Copa do Mundo FIFA 2026 da BBC Sport.

Converte o HTML de https://www.bbc.com/sport/football/world-cup/schedule
em uma estrutura de dados Python (dicts/listas) pronta para ser servida
como JSON por uma API REST.

Estruturas extraídas:
  * Classificação dos grupos A..L
  * Tabela de melhores terceiros (3rd Place Ranking)
  * Jogos do mata-mata (Last 32, Last 16, Quarter-finals,
    Semi-finals, Final e Third Place)
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SOURCE_URL = "https://www.bbc.com/sport/football/world-cup/schedule"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Classes do BBC vêm no formato `ssrcss-<hash>-<NomeSemantico>` e o hash muda
# entre builds. Por isso fazemos a correspondência pelo SUFIXO semântico.
def has_class_suffix(tag: Tag, suffix: str) -> bool:
    if not tag.has_attr("class"):
        return False
    return any(c.endswith(suffix) or c == suffix for c in tag["class"])


def find_one(node: Tag, suffix: str) -> Optional[Tag]:
    for el in node.find_all(True):
        if has_class_suffix(el, suffix):
            return el
    return None


def find_all_suffix(node: Tag, suffix: str):
    return [el for el in node.find_all(True) if has_class_suffix(el, suffix)]


def _remove_invisible(tag: Tag) -> Tag:
    """Copia um nodo removendo tudo que é visualmente escondido (screen-reader)."""
    import copy
    clone = copy.copy(tag)
    for hid in clone.find_all(True):
        if has_class_suffix(hid, "VisuallyHidden") or has_class_suffix(hid, "visually-hidden"):
            hid.decompose()
    return clone


def visible_text(tag: Optional[Tag]) -> str:
    if tag is None:
        return ""
    return _remove_invisible(tag).get_text(" ", strip=True)


def clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
#  Modelos
# --------------------------------------------------------------------------- #
@dataclass
class TeamStanding:
    position: int
    team: str
    team_slug: Optional[str]
    flag: Optional[str]
    played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    goals_for: int = 0
    goals_against: int = 0
    goal_difference: int = 0
    points: int = 0
    form: list[str] = field(default_factory=list)

    def __post_init__(self):
        for attr in ("position", "played", "won", "drawn", "lost",
                     "goals_for", "goals_against", "goal_difference", "points"):
            try:
                setattr(self, attr, int(getattr(self, attr) or 0))
            except (TypeError, ValueError):
                setattr(self, attr, 0)


@dataclass
class Match:
    round: str
    state: str           # "completed" | "scheduled"
    status_label: str    # "Full time", "Scheduled", "Penalty shootout", etc.
    status_code: str     # "FT", "Scheduled", "PENS", "AET"
    home_team: str
    home_slug: Optional[str]
    home_flag: Optional[str]
    home_is_placeholder: bool
    away_team: str
    away_slug: Optional[str]
    away_flag: Optional[str]
    away_is_placeholder: bool
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    home_regulation_score: Optional[int] = None   # 90min/ET (quando houve pênaltis)
    away_regulation_score: Optional[int] = None
    decided_by: Optional[str] = None              # "penalties" | "extra_time" | None
    venue: Optional[str] = None
    kickoff_utc: Optional[str] = None             # ISO 8601 ( atributo dateTime )
    link: Optional[str] = None
    event_id: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
#  Grupo de classificação
# --------------------------------------------------------------------------- #
def parse_standing_row(tr: Tag) -> TeamStanding:
    cells = tr.find_all("td")
    if not cells:
        raise ValueError("linha sem células")

    team_cell = cells[0]
    rank_el = find_one(team_cell, "-Rank")

    # identifica equipe pelo badge container ( badge-container-<slug> )
    badge = None
    for d in team_cell.find_all("div"):
        tid = d.get("data-testid", "")
        m = re.match(r"badge-container-(.+)", tid)
        if m:
            badge = d
            break
    slug = None
    flag = None
    if badge is not None:
        slug = re.match(r"badge-container-(.+)", badge["data-testid"]).group(1)
        img = badge.find("img")
        if img is not None:
            flag = img.get("src")

    name = ""
    link = team_cell.find("a", href=True)
    if link is not None:
        vh = link.find("span", attrs={"class": re.compile(r"visually-hidden|VisuallyHidden")})
        if vh is not None:
            name = vh.get_text(strip=True)
        if not name:
            name = link.get_text(strip=True)
        if not slug:
            m = re.search(r"/teams/([^/?#]+)$", link["href"])
            if m:
                slug = m.group(1)
    if not name:
        name = visible_text(team_cell)

    # mapeia as demais células por aria-label
    fields = {}
    for c in cells[1:]:
        lbl = c.get("aria-label")
        if not lbl:
            continue
        fields[lbl] = visible_text(c).strip()

    # form
    form_cell = None
    for c in cells:
        if c.get("aria-label", "").startswith("Form"):
            form_cell = c
            break
    form = []
    if form_cell is not None:
        for li in form_cell.find_all("li"):
            vh = li.find("span", attrs={"class": re.compile(r"visually-hidden|VisuallyHidden")})
            if vh is not None and vh.get_text(strip=True):
                form.append(vh.get_text(strip=True))
            else:
                form.append(visible_text(li) or "No Result")

    return TeamStanding(
        position=int(rank_el.get_text(strip=True)) if rank_el is not None else 0,
        team=clean_name(name),
        team_slug=slug if slug != "null" else None,
        flag=flag,
        played=int(fields.get("Played") or 0),
        won=int(fields.get("Won") or 0),
        drawn=int(fields.get("Drawn") or 0),
        lost=int(fields.get("Lost") or 0),
        goals_for=int(fields.get("Goals For") or 0),
        goals_against=int(fields.get("Goals Against") or 0),
        goal_difference=int(fields.get("Goal Difference") or 0),
        points=int(fields.get("Points") or 0),
        form=form,
    )


def parse_group_table(table: Tag) -> list[TeamStanding]:
    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")
    standings = []
    for tr in rows:
        try:
            standings.append(parse_standing_row(tr))
        except Exception:
            continue
    return standings


def heading_text(section_or_div: Tag) -> str:
    h = section_or_div.find(["h2", "h3"])
    return clean_name(h.get_text()) if h is not None else ""


def parse_groups(soup: BeautifulSoup) -> list[dict]:
    group_section = soup.select_one('section[role="tabpanel"][id="GroupStage"]')
    if group_section is None:
        group_section = soup
    tables = group_section.select('table[data-testid="football-table"]')
    groups = []
    for tbl in tables:
        # heading fica num ancestral próximo
        parent = tbl
        title = ""
        for _ in range(6):
            parent = parent.parent
            if parent is None:
                break
            h = parent.find(["h2", "h3"], recursive=False) or parent.find(["h2", "h3"])
            if h is not None:
                title = clean_name(h.get_text())
                break
        standings = parse_group_table(tbl)
        if title.lower().startswith("3rd"):
            groups.append({
                "id": "3rd",
                "name": title or "3rd Place Ranking",
                "type": "third_place_ranking",
                "standings": [asdict(s) for s in standings],
            })
        else:
            letter = ""
            m = re.search(r"Group\s*([A-L])", title, re.IGNORECASE)
            if m:
                letter = m.group(1).upper()
            groups.append({
                "id": letter or clean_name(title),
                "name": title or "Group",
                "type": "group",
                "standings": [asdict(s) for s in standings],
            })
    return groups


# --------------------------------------------------------------------------- #
#  Mata-mata
# --------------------------------------------------------------------------- #
def parseparticipant(part: Tag):
    is_placeholder = False
    slug = None
    flag = None
    badge = None
    for d in part.find_all("div"):
        tid = d.get("data-testid", "")
        m = re.match(r"badge-container-(.+)", tid)
        if m:
            badge = d
            break
    if badge is not None:
        slug = re.match(r"badge-container-(.+)", badge["data-testid"]).group(1)
        if slug == "null":
            is_placeholder = True
            slug = None
        img = badge.find("img")
        if img is not None:
            flag = img.get("src")

    name_el = find_one(part, "-Nickname")
    name = ""
    if name_el is not None:
        name = clean_name(name_el.get_text(" ", strip=True))
    if not name:
        vh = part.find("span", attrs={"class": re.compile(r"visually-hidden|VisuallyHidden")})
        if vh is not None:
            name = clean_name(vh.get_text())
    if not name:
        name = clean_name(visible_text(part))

    # score principal
    score = None
    score_el = find_one(part, "-Score")
    if score_el is not None:
        txt = visible_text(score_el)
        m = re.match(r"(\d+)", txt)
        if m:
            score = int(m.group(1))

    secondary = None
    secondary_el = find_one(part, "-SecondaryScore")
    if secondary_el is not None:
        txt = visible_text(secondary_el)
        m = re.match(r"(\d+)", txt)
        if m:
            secondary = int(m.group(1))

    return {
        "name": name,
        "slug": slug,
        "flag": flag,
        "is_placeholder": is_placeholder,
        "score": score,
        "regulation_score": secondary,
    }


def parse_knockout_match(container: Tag, round_name: str) -> Optional[Match]:
    event_id = container.get("data-event-id")

    # status
    status_label = visible_text(container.find(attrs={"data-testid": "h2h-accessible-comment"})) or ""
    status_code = visible_text(container.find(attrs={"data-testid": "h2h-comment"})) or status_label
    secondary_comment = visible_text(container.find(attrs={"data-testid": "h2h-secondary-comment"})) or ""

    # determina estado e método
    if status_code.upper() == "FT" or "full time" in status_label.lower():
        state = "completed"
        decided_by = None
    elif status_code.upper() in ("PENS", "PEN") or "penalt" in status_label.lower():
        state = "completed"
        decided_by = "penalties"
    elif "aet" in secondary_comment.lower() or status_code.upper() == "AET":
        # Se houve pênaltis já capturamos acima; se só AET, é extra time
        state = "completed"
        decided_by = decided_by if "decided_by" in dir() else "extra_time"
    else:
        state = "scheduled"
        decided_by = None

    # Ajuste fino: "PENS" + "AET" => decided_by = penalties
    if status_code.upper() == "PENS" and "aet" in secondary_comment.lower():
        decided_by = "penalties"
    elif status_code.upper() == "AET":
        decided_by = "extra_time"

    # participantes
    parts = find_all_suffix(container, "-StyledParticipant")
    if len(parts) < 2:
        return None
    home = parseparticipant(parts[0])
    away = parseparticipant(parts[1])

    # venue
    venue = None
    v = find_one(container, "-VenueWrapper")
    if v is not None:
        venue = clean_name(v.get_text()).replace("Venue:", "").strip()

    # kickoff time
    kickoff_utc = None
    t = container.find("time")
    if t is not None:
        kickoff_utc = t.get("dateTime") or t.get("datetime") or t.get("dateTime=")

    # link
    link = None
    a = container.find_parent("a", href=True)
    if a is not None:
        href = a["href"]
        m = re.search(r"/sport/football/live/(\S+)", href)
        if m:
            link = "https://www.bbc.com" + href if href.startswith("/") else href
    if link is None:
        a = container.find("a", href=True)
        if a is not None and "live" in a["href"]:
            link = "https://www.bbc.com" + a["href"]

    # scores
    home_score = home["score"] if home["score"] is not None else None
    away_score = away["score"] if away["score"] is not None else None
    home_reg = home["regulation_score"]
    away_reg = away["regulation_score"]

    # se não foi decidido nos pênaltis, não há regulation_score separado
    if decided_by not in ("penalties",):
        home_reg = None
        away_reg = None

    return Match(
        round=round_name,
        state=state,
        status_label=status_label or status_code,
        status_code=status_code,
        home_team=home["name"],
        home_slug=home["slug"],
        home_flag=home["flag"],
        home_is_placeholder=home["is_placeholder"],
        away_team=away["name"],
        away_slug=away["slug"],
        away_flag=away["flag"],
        away_is_placeholder=away["is_placeholder"],
        home_score=home_score,
        away_score=away_score,
        home_regulation_score=home_reg,
        away_regulation_score=away_reg,
        decided_by=decided_by,
        venue=venue,
        kickoff_utc=kickoff_utc,
        link=link,
        event_id=event_id,
    ).to_dict()


def parse_knockout(soup: BeautifulSoup) -> list[dict]:
    k = soup.select_one('#KnockoutStage') or soup
    if k is None:
        return []

    rounds = []
    # itera por coluna de round
    columns = []

    # colunas com título ( Last 32, Last 16, Quarter-finals, Semi-finals )
    for col in k.find_all(True):
        if not has_class_suffix(col, "-TournamentColumnWrapper"):
            continue
        title_el = find_one(col, "-KnockoutStageTitle")
        title = clean_name(title_el.get_text()) if title_el is not None else ""
        if title:
            columns.append((title, col))
        else:
            # coluna final/third place (sem título KnockoutStageTitle)
            columns.append(("Final / Third Place", col))

    # O painel Final/ThirdPlace existe como uma coluna separada; se não houver
    # título, detectamos pelos wrappers internos.
    matches = []
    for title, col in columns:
        # descobre wrappers de partida dentro da coluna
        wrappers = col.find_all(attrs={"data-event-id": True})
        seen = set()
        for w in wrappers:
            if id(w) in seen:
                continue
            seen.add(id(w))
            # round name
            if title == "Final / Third Place":
                # descobre se é final ou third place via ancestral interno
                anc = w
                rnd = "Final"
                while anc is not None:
                    if has_class_suffix(anc, "-Final"):
                        rnd = "Final"
                        break
                    if has_class_suffix(anc, "-ThirdPlacePlayOff"):
                        rnd = "Third Place"
                        break
                    anc = anc.parent
                    if anc is col:
                        break
            else:
                rnd = title
            m = parse_knockout_match(w, rnd)
            if m is not None:
                # ajusta decided_by para AET quando só vier AET
                matches.append(_normalize_match(m))

    return matches


def _normalize_match(m: dict) -> dict:
    # se estado agendado e sem placar, mantém None
    if m["state"] == "scheduled":
        m["home_score"] = None
        m["away_score"] = None
        m["home_regulation_score"] = None
        m["away_regulation_score"] = None
        m["decided_by"] = None
    elif m["decided_by"] == "penalties":
        # score principal já é o de pênaltis
        pass
    elif m["decided_by"] == "extra_time":
        # score principal é o final após ET; não há regulation separada
        m["home_regulation_score"] = None
        m["away_regulation_score"] = None
    else:
        # full time normal
        m["home_regulation_score"] = None
        m["away_regulation_score"] = None
    return m


# --------------------------------------------------------------------------- #
#  Coleta principal
# --------------------------------------------------------------------------- #
def fetch_html(url: str = SOURCE_URL, timeout: int = 30) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_all(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    groups = parse_groups(soup)
    knockout = parse_knockout(soup)

    # separa grupos / 3rd
    group_list = [g for g in groups if g["type"] == "group"]
    third = next((g for g in groups if g["type"] == "third_place_ranking"), None)

    return {
        "source": SOURCE_URL,
        "competition": "FIFA World Cup 2026",
        "groups": group_list,
        "third_place_ranking": third,
        "knockout": _group_knockout_by_round(knockout),
        "matches": knockout,
    }


def _group_knockout_by_round(matches: list[dict]) -> dict:
    out: dict[str, list[dict]] = {}
    for m in matches:
        out.setdefault(m["round"], []).append(m)
    return out


# CLI rápido para teste sem servidor
if __name__ == "__main__":
    import pathlib
    if len(sys.argv) > 1 and sys.argv[1] == "--file":
        html = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8", errors="ignore")
    else:
        html = fetch_html()
    data = parse_all(html)
    print(json.dumps(data, ensure_ascii=False, indent=2)
          [:4000])  # preview das 1ªs linhas
    print("\n--- RESUMO ---")
    print(f"Grupos: {len(data['groups'])}")
    for g in data["groups"]:
        print(f"  Group {g['id']}: {len(g['standings'])} times")
    print(f"Partidas mata-mata: {len(data['matches'])}")
    for rnd, ms in data["knockout"].items():
        print(f"  {rnd}: {len(ms)}")