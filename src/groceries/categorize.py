"""Category inference for products the retailer gives no category.

Jumbo's product search returns no category, and some Albert Heijn receipt items cannot be mapped to
the webshop catalogue, so those products land in the unknown bucket. This module fills the gap
locally, without network calls:

1. **Keyword rules**: common Dutch grocery words in the title or the receipt name ("KAAS", "DEO",
   "AFWAS", "SUSHI"). The rules are ordered, specific before general, and the first that matches wins.
2. **Similar products**: the categorized product (of any retailer) whose title shares the most words.

The categories are Albert Heijn's (the only taxonomy the app has); a rule's category is mapped to the
spelling already in the database. Inferred categories are stored with ``category_inferred = 2`` and
worked out again on every run, so better rules apply to them; a category from the retailer always
wins and is never overwritten.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

LOCAL = 2  # products.category_inferred: 0 catalogue, 1 retailer search match, 2 inferred here

# (pattern on the upper-case name, category). Patterns match at word starts; order matters.
RULES: list[tuple[str, str]] = [
    # not food
    (
        r"BATTERIJ|OPLAADBAAR|\bAAA\b|\bAA\b|LAMP\b|LED\b|KAARS|KNUTSEL|LIJM\b|SOCK|SOKKEN|POCKET|D ?DUCK|TIJDSCHRIFT"
        r"|SPEELGOED|PLAYTOPIA|CADEAU|BRABANTIA|CAN COOLER|SNACKBOX|BEWAARDOOS",
        "Koken, tafelen, vrije tijd",
    ),
    (
        r"\w*BLOEMEN|BLOEM\b|ROZEN|ROZENBK|TULP|CHRYSANT|LAVEND P|PLANT\b|POTGROND|SEIZOENSMIX|BOEKET",
        "Koken, tafelen, vrije tijd",
    ),
    (r"HONDEN|KATTEN|KITTEN|WHISKAS|FELIX|PEDIGREE|SHEBA|KATTENBAK|DIERENVOER", "Huisdier"),
    (r"LUIER|PAMPERS|BABYDOEK|BILDOEK|OLVARIT|NUTRILON|BABY", "Baby en kind"),
    (
        r"DEO\b|DEODORANT|LISTERINE|MONDWATER|MW\b|TANDPASTA|TANDENBORSTEL|SHAMPOO|DOUCHE|HANDZEEP|ZEEP|SCHEER"
        r"|MAANDVERBAND|TAMPON|PLEISTER|PARACETAMOL|IBUPROFEN|VITAMINE|CREME\b|BODYLOTION|DOVE\b|NIVEA|ZONNEBRAND",
        "Drogisterij",
    ),
    (
        r"AFWAS|VAATWAS|WASMIDDEL|\bWM\b|LENOR|ROBIJN|ARIEL|PERSIL|WASVERZACHTER|TOILETPAPIER|WC ?PAPIER|KEUKENROL"
        r"|KUSSENZACHT|PAGE\b|ZAKDOEK|TISSUE|ALLESREINIGER|SCHOONMAAK|VUILNISZAK|AFVALZAK|LUSDRAAGTAS|DRAAGTAS|TAS\b"
        r"|SPONS|VAATDOEK|BLEEK|ONTKALKER|ALUFOLIE|BAKPAPIER|HUISHOUD",
        "Huishouden",
    ),
    # drinks
    (
        r"\bBIER\b|PILSENER|PILS\b|HEINEKEN|GROLSCH|AMSTEL|RADLER|\bIPA\b|WIJN|\bWIJN|ROSE\b|PROSECCO|CAVA\b|CHAMPAGNE"
        r"|SAKE\b|WHISKY|WODKA|VODKA|GIN\b|RUM\b|PORT\b|MERLOT|CHARDONNAY|SAUVIGNON|SHIRAZ|MALBEC",
        "Bier, wijn, aperitieven",
    ),
    (r"KOFFIE|CAPSULE|NESPRESSO|SENSEO|ESPRESSO|LUNGO|\bTHEE\b|PICKWICK|LIPTON|THEEZAK", "Koffie, thee"),
    (
        r"FRISDRANK|COLA|FANTA|SPRITE|7UP|SPA\b|BRUISWATER|MINERAALWATER|\bWATER\b|SAP\b|JUICE|SMOOTHIE|LIMONADE|SIROOP"
        r"|ICE ?TEA|ENERGY|ENERGIS|INNOC|CHAUDFONTAINE|APPELSAP|SINAASAPPELSAP|DUBBELFRISS|CASSIS",
        "Frisdrank, sappen, water",
    ),
    # fresh and chilled
    (
        r"PASTASAUS|SAUS\b|SAUS|KETCHUP|MAYO|MAYONAISE|MOSTERD|DRESSING|PESTO|BOUILLON|SOEP\b|SOEP|OLIJFOLIE|\bOLIE\b"
        r"|AZIJN|KRUIDEN|PEPER|ZOUT\b|SAMBAL|KETJAP|SPECERIJ|KRUIDENMIX",
        "Soepen, sauzen, kruiden, olie",
    ),
    (
        r"HUMMUS|BABAGANOUSH|TZATZIKI|\w*SALADE|MAALTIJD|LASAGNE|OVENSCHOTEL|PIZZA|QUICHE|NOODLE ?CUP|GEVULD EI",
        "Maaltijden, salades",
    ),
    (
        r"SUSHI|MAKI\b|NIGIRI|ZALM|TONIJN|GARNAL|EBI\b|KABELJAUW|PANGASIUS|HARING|MAKREEL|MOSSEL|VIS\b|VISSTICK|SCAMPI"
        r"|FOREL|KIBBELING|LEKKERBEK|SURIMI",
        "Vis",
    ),
    (
        r"HAM\b|RAUWE HAM|BACON|SALAMI|CHORIZO|ROSBIEF|ROSBEEF|ROOKVLEES|FILET AMERICAIN|LEVERWORST|BOTERHAMWORST"
        r"|SERRANO|PROSCIUTTO|CERVELAAT|KIPFILET BELEG|PASTRAMI|SNIJWORST|PATE\b",
        "Vleeswaren",
    ),
    (
        r"KIP\b|KIPFILET|KIPDIJ|KIPPENDIJ|KIPPENBOUT|DRUMSTICK|KALKOEN|SCHNITZEL|CORDON|GEHAKT|HAMBURGER|BIEFSTUK"
        r"|RUNDVLEES|VARKENS|SPEKLAP|KARBONADE|SHOARMA|SATE\b|SATÉ|WORST\b|ROOKWORST|BRAADWORST|SUCADE|OSSENHAAS"
        r"|LAMSVLEES|RIBEYE|BALLETJES",
        "Vlees",
    ),
    (
        r"KAAS|KAZEN|CAMEMB|BRIE\b|GOUDA|GEITENKAAS|CHAVROUX|MOZZARELLA|PARMEZAAN|PARMIGIANO|FETA\b|BURRATA|ROOMKAAS"
        r"|PATURAIN|PHILADELPHIA|MASCARPONE|RICOTTA|MORBIER|LEERDAMMER|COMTE|GRUYERE|ROQUEFORT|HALLOUMI|BABYBEL",
        "Kaas",
    ),
    (
        r"MELK\b|MELK|KARNEMELK|YOGHURT|KWARK|VLA\b|SLAGROOM|ROOM\b|KOOKROOM|CREME FRAICHE|BOTER\b|ROOMBOTER|HALVARINE"
        r"|BLUE BAND|\bEI\b|EIEREN|SCHARREL ?EI|SCHARRELEI|SKYR|ALPRO|HAVERDRANK|DESSERT|PUDDING|TOETJE|CUSTARD",
        "Zuivel, eieren",
    ),
    (
        r"\w*BROOD|BOLLETJE|BOL\b|PISTOLET|STOKBROOD|BAGUETTE|CROISSANT|CROU\b|BEGEL|BAGEL|WRAP|TORTILLA|PITA\b|BEREN"
        r"|KRENTENBOL|BOTERKOEK|TAART|GEBAK|MUFFIN|DONUT|APPELFLAP|BRIOCHE|CIABATTA|FOCACCIA|BANANABREAD|KADET",
        "Bakkerij",
    ),
    (
        r"KOEK|KOEKJE|BISCUIT|STROOPWAFEL|PIM'?S|OREO|LU\b|SPECULAAS|ONTBIJTKOEK|CHOCOLA|CHOCO|TONY|MILKA|M&M"
        r"|SNOEP|DROP|WINEGUM|SPEKJES|LOLLY|KAUWGOM|MENTOS|ZOO ORIGINAL|LEIBNIZ|WAFEL|TOMPOUCE|PEPERNOT|KRUIDNOT"
        r"|CHOCOLADELETTER|BONBON|NUTELLA",
        "Koek, snoep, chocolade",
    ),
    (
        r"APPEL|PEER\b|PEREN|BANAAN|BANANEN|SINAASAPPEL|MANDARIJN|CLEMENTINE|DRUIF|DRUIVEN|AARDBEI|FRAMBO"
        r"|BESSEN|BLAUWE BES|KIWI|MANGO|ANANAS|\w*MELOEN|CITROEN|LIMOEN|AVOCADO|PERZIK|NECTARINE"
        r"|PRUIM|KERS|GRANAATAPPEL"
        r"|ZACHTFRUIT|FRUITMIX|FRUIT\b|VIJG|DADEL",
        "Fruit, verse sappen",
    ),
    (
        r"\w*TOMAAT|\w*TOMATEN|\w*TOMAATJES|\w*KOMKOMMER|PAPRIKA|SLA\b|IJSBERG|RUCOLA|SPINAZIE|BROCCOLI|BLOEMKOOL|COURGETTE"
        r"|AUBERGINE|WORTEL|PREI|UI\b|UIEN|KNOFLOOK|CHAMPIGNON|PADDENSTOEL|AARDAPPEL|KRIEL|ZOETE AARDAPPEL|SPRUITEN"
        r"|BOONTJES|SPERZIE|ASPERGE|BIET|PASTINAAK|POMPOEN|RAUWKOST|SNACKGROENTE|GROENTE|ANDIJVIE|BOERENKOOL|RADIJS"
        r"|SELDERIJ|VENKEL|MAIS\b|PEULTJES",
        "Groente, aardappelen",
    ),
    (
        r"PASTA\b|SPAGHETTI|PENNE|FUSILLI|FARFALLE|TAGLIATELLE|TORTELLONI|TORTELLINI|RAVIOLI|MACARONI|LASAGNEBLAD"
        r"|NOEDEL|NOODLE|RIJST|RISOTTO|COUSCOUS|BULGUR|QUINOA|POLENTA|BAMI|NASI|WOK|CURRY|KOKOSMELK|TACO|BURRITO",
        "Pasta, rijst, wereldkeuken",
    ),
    (
        r"OLIJF|OLIJVEN|KALAMATA|AUGURK|ZILVERUI|TAPENADE|PINDA|NOTEN|CASHEW|AMANDEL|CHIPS|PRINGLES|NACHO"
        r"|ZOUTJES|ZOUTE|TUC\b|BORREL|BITTERBAL|KAASSTENGEL|POPCORN",
        "Borrel, chips, snacks",
    ),
    (
        r"MUESLI|CRUESLI|GRANOLA|HAVERMOUT|CORNFLAKES|ONTBIJTGRAAN|BRINTA|JAM\b|HAGELSLAG|PINDAKAAS|HONING\b|STROOP"
        r"|BELEG|VRUCHTENHAGEL|CHOCOPASTA|APPELSTROOP",
        "Ontbijtgranen, beleg",
    ),
    (r"IJS\b|IJSJE|MAGNUM|CORNETTO|DIEPVRIES|FRIET|PATAT|KROKET|FRIKANDEL|SNACKBOX", "Diepvries"),
    (r"REEP|MUESLIREEP|PROTEINE|EIWIT|RIJSTWAFEL|SNACKBAR|NUTRI|LIGA", "Tussendoortjes"),
]
_COMPILED = [(re.compile(r"(?<![A-Z])(?:" + p + ")"), c) for p, c in RULES]

# words that say little about the category: brands, packaging, sizes, adjectives
STOP = {
    "AH",
    "JUMBO",
    "JUMBOS",
    "JUMBO'S",
    "BIOLOGISCH",
    "BIO",
    "EXCELLENT",
    "BASIC",
    "TERRA",
    "VERS",
    "VERSE",
    "GROOT",
    "KLEIN",
    "MINI",
    "MAXI",
    "STUKS",
    "STUK",
    "PAK",
    "PACK",
    "LIGHT",
    "NATUREL",
    "NATURAL",
    "ORIGINAL",
    "MET",
    "VAN",
    "EN",
    "DE",
    "HET",
    "IN",
    "OP",
    "VOOR",
    "ZONDER",
    "EXTRA",
    "SUPER",
    "VOORDEEL",
    "VOORDEELPAK",
    "HALF",
    "HALVE",
    "GRAM",
    "LITER",
    "PER",
    "DUO",
    "TRIO",
    "MIX",
    "STAR",
    "SELECTION",
    "FAMILIE",
    "FAMILY",
}


def _words(text: str) -> set[str]:
    return {
        w
        for w in re.split(r"[^0-9A-ZÀ-Ü']+", (text or "").upper())
        if len(w) >= 3 and not any(c.isdigit() for c in w) and w not in STOP
    }


def rule_category(text: str) -> str | None:
    up = (text or "").upper()
    for rx, cat in _COMPILED:
        if rx.search(up):
            return cat
    return None


@dataclass
class Guess:
    category: str
    how: str  # "rule" or "similar"


class Categorizer:
    """Infers a category from the categorized products in the database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT COALESCE(title, name) AS t, category FROM products "
            "WHERE category IS NOT NULL AND category_inferred != ? AND category != 'Koopzegels'",
            (LOCAL,),
        ).fetchall()
        self.known = [(_words(t), c) for t, c in rows if _words(t)]
        self.index: dict[str, list[int]] = defaultdict(list)
        for i, (ws, _c) in enumerate(self.known):
            for w in ws:
                self.index[w].append(i)
        # map a rule's category to the spelling in use (AH renames categories now and then)
        existing = {c for _w, c in self.known}
        self.names: dict[str, str] = {}
        for _rx, cat in _COMPILED:
            if cat in existing:
                self.names[cat] = cat
            else:
                first = cat.split(",")[0].split(" ")[0].lower()
                same = sorted(c for c in existing if c.lower().startswith(first))
                self.names[cat] = same[0] if same else cat

    def similar(self, text: str) -> str | None:
        ws = _words(text)
        if not ws:
            return None
        scores: Counter[int] = Counter()
        for w in ws:
            for i in self.index.get(w, ()):
                scores[i] += 1
        best: list[tuple[float, str]] = []
        for i, shared in scores.items():
            other, cat = self.known[i]
            jac = shared / len(ws | other)
            if jac >= 0.5 or (shared >= 2 and jac >= 0.34):
                best.append((jac, cat))
        if not best:
            return None
        top = max(j for j, _c in best)
        votes = Counter(c for j, c in best if j >= top - 0.05)
        (cat, n), *rest = votes.most_common(2) + [("", 0)]
        return cat if n > rest[0][1] else None  # a tie between categories: no guess

    def guess(self, title: str | None, name: str | None) -> Guess | None:
        # the receipt name is what was bought; the title can come from a wrong search match, but it is
        # cleaner than the cut-short receipt name, so it counts when the receipt name says nothing
        cat = rule_category(name or "") or rule_category(title or "")
        if cat:
            return Guess(self.names.get(cat, cat), "rule")
        for text in (title, name):
            if text and (cat := self.similar(text)):
                return Guess(cat, "similar")
        return None


def infer_categories(conn: sqlite3.Connection, dry_run: bool = False) -> dict[str, int]:
    """Fill in categories for products without one. Returns counts per outcome."""
    cz = Categorizer(conn)
    rows = conn.execute(
        """SELECT retailer, product_key, title, name, category FROM products
           WHERE (category IS NULL OR category_inferred = ?) AND product_key != 'stamps'""",
        (LOCAL,),
    ).fetchall()
    counts = {"checked": len(rows), "rule": 0, "similar": 0, "unknown": 0, "changed": 0}
    updates: list[tuple[str | None, int, str, str]] = []
    for retailer, key, title, name, current in rows:
        g = cz.guess(title, name)
        counts[g.how if g else "unknown"] += 1
        new = g.category if g else None
        if new != current:
            counts["changed"] += 1
            updates.append((new, LOCAL if new else 0, retailer, key))
    if updates and not dry_run:
        with conn:
            conn.executemany(
                "UPDATE products SET category = ?, category_inferred = ? WHERE retailer = ? AND product_key = ?",
                updates,
            )
    return counts
