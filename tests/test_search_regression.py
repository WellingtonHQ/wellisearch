"""Search regression suite (needs Postgres): fixed dataset + queries -> expected results.

Runs against a dedicated throwaway database (wellisearch_test — dropped and
rebuilt on every run) so expectations are deterministic regardless of what the
dev index contains. The provider gateway is stubbed, so no network or API keys
are needed:

  python tests/test_search_regression.py

Scenarios pinned to the dataset below:
  Q1 on-topic query, a full set passes all gate conditions -> served local (zero credits)
  Q2 first misses, then one strong provider result is indexed -> repeat served local
  Three moderate passing rows, no strong row               -> partial set served local (per-domain cap trims the third example.com row)
  Marginal partial row passes ordinary gate but not strong gate -> provider
  Off-brand pages clear coverage+similarity but miss every distinctive word -> provider
  An on-brand page (distinctive_coverage 1.0) serves; off-brand companions dropped
  Q3 word dump has coverage 1.0 but no topical similarity  -> deferred to the provider
  Q4 shopping query with a qualifier and a price term:
     the catalog covers 2/4 of {best, costco, dishwash, 700}, the price guide
     covers 3/4; only one cleared the old 0.75 bar, but both clear 0.5       -> served local
  Q2 with search_mode="local"                              -> index rows, gate bypassed
"""
from __future__ import annotations

import asyncio
import os

# host-local endpoint + dedicated test DB (container aliases don't resolve on
# the host; a throwaway DB keeps expectations deterministic against the fixed
# dataset below)
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ["POSTGRES_DB"] = "wellisearch_test"

import psycopg  # noqa: E402

from wellisearch import search_web as sw  # noqa: E402
from wellisearch.config import get_settings  # noqa: E402
from wellisearch.db import db  # noqa: E402
from wellisearch.embed import embed_one  # noqa: E402
from wellisearch.index import store_page  # noqa: E402
from wellisearch.providers.base import Result  # noqa: E402

# ---------------------------------------------------------------------------
# Dataset (fixed URLs + texts; the expectations below are pinned to them)
# ---------------------------------------------------------------------------

CAR_SEAT_A = "https://example.com/car-seat-newborn-head"
CAR_SEAT_B = "https://example.com/rear-facing-car-seat-bumpy-road"
WORD_DUMP = "https://example.com/common-word-list"
APOLLO_1202 = "https://example.com/apollo-11-1202-alarm"
DOORBELL_FAQ = "https://example.com/smart-doorbell-alarm-faq"
DISHWASHER_A = "https://example.com/costco-dishwasher-catalog"
DISHWASHER_B = "https://example.com/dishwasher-price-guide"

# Q1: 12 content words; both car-seat pages contain every one of them.
Q1 = "newborn head bobbing bouncing bumpy road car seat normal safe rear facing"
# Q2: 14 content words (including the numerals 11 and 1202); only the Apollo
# page contains all of them, so a full set of k=5 can never pass.
Q2 = "Apollo 11 1202 program alarm priority interrupt AGC proximity sensor Margaret Hamilton lunar descent"
# Q3: topical words that appear in the word dump but are concentrated on no article page.
Q3 = "quantum physics black hole telescope galaxy"
# Q4: lexemes {best, costco, dishwash, 700} — Postgres retains "700" but
# drops "under". Both pages contain costco + dishwasher but not "best";
# the catalog omits 700 (coverage 2/4 = 0.5) and the price guide mentions it
# (coverage 3/4 = 0.75). Only one cleared the old 0.75 bar, so k=2 deferred.
Q4 = "Best Costco Dishwasher under $700"

CAR_SEAT_A_MD = """\
# Why a newborn's head flops forward in the car seat

A newborn's head is proportionally heavy and the neck muscles are not strong enough to hold it upright. On a bumpy road the bobbing and bouncing of the ride can make an infant's head flop forward or loll to one side, which looks alarming but is normal for the first months. The question most parents ask is whether this is safe: in a properly installed rear facing car seat, yes — the harness keeps the body supported while the head moves within a small range. Choose a car seat with a deep headrest and side impact protection, recline it to the manufacturer's angle so the airway stays open, and avoid adding bulky blankets that let the head sink forward. On long drives over rough or bumpy road surfaces, take breaks at rest stops so the baby can be held upright. Rear facing is the safest orientation for a newborn because it supports the head, neck, and spine in a crash. If you notice excessive bobbing even on smooth roads, check that the harness straps are snug and the seat is not over-reclined.
"""

CAR_SEAT_B_MD = """\
# Keeping a baby's head supported in a rear facing car seat on bumpy roads

Infants ride best in a rear facing car seat until at least age two. The main comfort and safety issue is the head: on a bumpy road every bump becomes bobbing and bouncing, and a newborn's head can flop forward toward the chest. This is normal and safe when the seat is installed correctly — the key is keeping the head supported without blocking the airway. Use the seat's built-in headrest at the correct height, recline the car seat so the baby lies nearly flat, and avoid aftermarket padding behind the head that changes the crash geometry. If your route includes rough or bumpy road sections, a rear facing position with a snug harness absorbs most of the motion. Never let the head fall fully forward for long; check the fit after every car trip and adjust as the baby grows.
"""

# A list of common English words (~2000 distinct): it contains every Q1 and Q3
# word (coverage 1.0) but has no topical coherence, so its best-chunk similarity
# stays far below any real article's — exactly the failure mode the similarity
# gate exists for (the live-index BERT vocab dump measured -0.03..0.10).
WORD_DUMP_MD = """\
# Common English word list

ability able about above accept account across act action activity actually add address admit adopt affect after again against age agency agent ago agree ahead air allow almost alone along already also always among annual answer another anyone anything appear apply approach area argue around arrive art artist ask assume attention audience author authority available avoid away baby back bad bag balance ball bank bar base basic basis beat become before begin behind being believe benefit best better between beyond big bill birth bit black block blood blue board body book born both box boy break bring brother brown build building business busy buy by call camera campaign candidate capital card care career careful carry case cash cat catch cause cell center central century chain chair challenge change chapter character charge chart chase check chief child choice choose church circle citizen city claim class clean clear close coach cold college color come commercial common community company compare complete computer concern condition conference connect consider contain continue control cook cool cost counter country couple course court cover create culture cup current customer cut dance data daughter day deal debate decide decision deep degree deliver demand demo depend deposit describe design desk detail determine develop development device die difference difficult digit dinner direct direction district divide doctor document dog door double doubt down draw dream drive drop during each early earn earth east easy eat economic economy edge effect effort eight either else end energy engine enjoy enough enter entire entry environment equal equipment era error especially even event everyone evidence exact example exchange exist expect experience expert explain eye face fact factor fail fall family financial find finger finish fire firm first fish fit five floor fly focus follow food foot force foreign forget form forward found four free friend from front full fund future game garden gas general generation get girl give glass go goal gold good government grab great green ground group grow growth guess gun hair half hand happen happy hard harm hat have head health hear heart heat heavy help high history hit hold home hope hospital hot hotel hour house human hundred hurt husband idea identify image impact improve include increase indicate industry information inside interest international involve issue item job join judge jump keep key kid kind kitchen knee know land language large last late launch law leader learn least leave left leg less letter level lie life light line link list listen little live local long look lose loss lot love low machine main major make man manage many mark market marriage mass master match material matter may mean measure media member memory mention message method middle might military million mind minute miss mission model moment money month more morning mother mouth move movie much music name nation national nature near need network never news next night north note nothing notice now number occur office often oil once one only open operation option order other out output outside over own page pain paint pair panel paper parent park part particular partner pass past patient pattern pay peace place plan plant play picture piece point police policy political poor popular population position positive power practice prepare pressure present president press pretty prevent previous price private probably problem process produce program project protect prove provide public pull purpose push quality question quick quiet quite race radio raise range rate rather reach read ready real realize reason receive recent recognize record reduce region relate release remain remember remove repeat replace report represent require research resource respond rest result return reveal rich rise risk road rock room rule run safe school science sea season seat second section security see sell send senior sense series service set seven several shake shape share sharp sheet shift shine short should show side sign signal similar simple since sing single sister sit site situation six size skill skin sleep slow small smile social society solution some soon sort sound source south space speak special specific spend sport spring staff stage stand standard star start state station stay step stick stock stone stop story straight strategy street strong structure study stuff style subject success such sudden suggest summer support suppose sure surface system table tail take talk target task tax team tear tell ten term test text than thank that the their them then there these they thing think this those though three through throw time tiny tip today together too top total touch town toward track trade train travel tree trial trip trouble trust truth try turn two type under understand unit until up upon us use usual very video view voice wait walk wall want war watch water way week weight west what when where which while white who why wide wife win wind window wine wing winter wire wish with woman word work world worry would write wrong year yes yet young your
ankle armpit artery bladder brow cheek elbow forearm forehead heel hip knuckle lip lung nail neck nose palm rib shoulder spine thigh toe wrist almond avocado carrot cinnamon cookie cucumber donut eggplant garlic ginger grape honey jam jelly lettuce lime mango melon mushroom olive onion oyster pecan pickle plum raisin spinach squash strawberry vanilla yogurt ant bee camel chicken cow crab frog goat lion monkey mouse owl pig rat sheep spider tiger whale zebra airport cinema classroom courthouse factory farm garage library market museum pharmacy restaurant stadium supermarket theater warehouse zoo actor accountant artist attorney baker banker barber builder butcher cashier chef clerk dentist engineer farmer firefighter judge lawyer mechanic nurse pilot plumber professor scientist soldier teacher waiter writer anchor axe battery broom chain curtain drawer fan fence frame hammer hook lamp ladder lock mirror needle pencil plate plug radio ruler scissors spoon switch towel wrench zipper achieve admit adopt agree allow analyze announce appear approve arrive ask assume attach attempt avoid begin believe belong build burn call carry catch change choose clean close collect compare complete compute connect consider contain continue control cook count create decide decrease defend deliver depend describe design destroy develop determine discuss discover display divide draw drop earn escape examine exist expand explain explore express extend fail follow forget form forward gain gather give grow happen hang hear help hide hold hope increase inform insist invite involve issue join judge keep know lead learn leave lend let lie lift listen live look lose love manage mean meet mention move name need open order own pack paint prepare present prevent print pull push put read receive record refer refuse regard register relate remain remove renew repair repeat report request require rest return reveal ride rise roll run save scan schedule search select sell send settle share shine shout show sing sink sit sleep slide smile smell snap spread spring stand start stay steal step stop stretch strike strip struggle study submit succeed suggest supply support survive take teach tell think throw touch train travel treat turn twist use vanish wait wake walk want wash watch wear welcome win wipe wish withdraw wonder work wrap worry
absolute academic acceptable accurate active additional advanced aggressive afraid amazing ancient angry anxious apparent average beautiful beneficial bitter blank blessed broad broken calm capable casual certain cheerful clever colorful confident convenient correct crazy critical curious dear delicate delicious dense dramatic dull eager elegant energetic enormous expensive extreme fair famous fatal final flat fresh friendly gentle giant glad global greedy gross harsh healthy holy huge hungry ideal important impressive incredible innocent intense interesting intelligent invisible ironic joyful keen legal lively lovely lucky magical massive mature mental mild mighty modern moral muddy musical mysterious narrow nasty neat nervous noisy notable obvious odd official optimistic ordinary painful pale perfect pleasant powerful precious prime proper quiet rare reasonable regular reliable remote ripe rough round rural salty sane scarce serious shiny silent sincere silly slim smooth solid sour spacious spare splendid steady subtle systematic tender terrible thirsty tidy tight tough tragic tranquil transparent tricky trivial typical ultimate unique universal urgent useful vast vivid weak weird wild wise witty
am pm dawn dusk midnight noon yesterday tomorrow today weekly monthly annually hourly daily one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million billion zero dozen half pair couple several many much few less least most more
beneath beside between beyond inside outside within without under over upon against along across around behind before during through toward up down front back top bottom center middle edge corner side surface interior exterior beige cyan gold gray indigo ivory khaki lavender lilac maroon navy peach purple rose rust salmon tan teal turquoise violet brown dew frost hail humidity lightning mist rainbow sleet sunshine thunder tornado weather airplane bicycle ferry helicopter jet motorbike scooter skateboard sleigh tram axle gear pedal tire
newborn bobbing bouncing bumpy normal rear facing car quantum physics hole telescope galaxy
"""

APOLLO_1202_MD = """\
# The Apollo 11 "1202 Program Alarm" explained

During the lunar descent of Apollo 11 on July 20, 1969, the guidance computer began throwing 1202 program alarms. A 1202 alarm means the AGC (Apollo Guidance Computer) had more work queued than it could finish in time — a priority interrupt condition where higher priority tasks starved the descent software. Flight director Gene Kranz and his team realized the cause: the rendezvous radar, which should have been switched off during powered descent, was still sending data through its proximity sensor interface, flooding the computer with extra work. Margaret Hamilton's team had insisted on a priority scheduling system that dropped low-priority tasks instead of failing, so the computer kept guiding the landing even while alarms flashed. The alarm sequence — 1202 followed by 1201 — lasted about 25 seconds and faded as the radar was turned off. Without that interrupt architecture, the first Moon landing would likely have been aborted.
"""

DOORBELL_FAQ_MD = """\
# Smart doorbell alarm system FAQ

**What does the alarm program do?** The alarm program arms your doorbell's motion sensor and chime when you leave home. You can set a priority for alerts so that person detections interrupt other notifications first. The device uses a wide-angle camera and two-way audio, and it works with most smart home hubs. Battery life is about three months per charge. If the sensor detects repeated motion while disarmed, the app sends a low-priority notification instead of an alarm.
"""

# Q4 fixtures: pages about Costco dishwashers that omit the qualifier "best";
# one mentions 700 and the other does not. A live Costco catalog variant
# measured coverage 0.5 / similarity 0.78 on the literal query.
DISHWASHER_A_MD = """\
# Dishwashers at Costco — full catalog

Shop the complete dishwasher lineup: built-in, freestanding, and compact models from Maytag, LG, Whirlpool, and KitchenAid. Prices start at $499 for a basic 14-place-setting freestanding unit and run to $1,299 for a connected stainless-steel built-in with third rack and quiet drive (as low as 44 dBA). Every model includes a six-year limited warranty on the motor and pump. Filter by capacity, noise level, and finish; most units ship free within two weeks, and in-store pickup is available at all warehouses. Energy Star ratings are listed per model, and this season's popular picks include the Maytag MDBH356 and the LG LFW24STVQ.
"""

DISHWASHER_B_MD = """\
# Dishwasher deals and price guide

Current dishwasher prices by tier: entry models with 14 place settings run $499–$649, mid-range units with third racks and quiet operation land at $700–$900, and premium connected built-ins top out near $1,300. What the price buys: stainless interiors replace plastic tubs around $550, inverter motors cut noise below 46 dBA past $750, and Wi-Fi control with leak detection appears above $800. Look for open-box pricing at warehouse clubs like Costco — a like-new mid-range unit often saves $150–$250 off list. Compare the energy guide before buying: an efficient model pays back its premium in about four years of utility savings.
"""

DATASET: list[tuple[str, str, str]] = [
    (CAR_SEAT_A, "Why a newborn's head flops forward in the car seat", CAR_SEAT_A_MD),
    (CAR_SEAT_B, "Keeping a baby's head supported in a rear facing car seat on bumpy roads", CAR_SEAT_B_MD),
    (WORD_DUMP, "Common English word list", WORD_DUMP_MD),
    (APOLLO_1202, 'The Apollo 11 "1202 Program Alarm" explained', APOLLO_1202_MD),
    (DOORBELL_FAQ, "Smart doorbell alarm system FAQ", DOORBELL_FAQ_MD),
    (DISHWASHER_A, "Dishwashers at Costco — full catalog", DISHWASHER_A_MD),
    (DISHWASHER_B, "Dishwasher deals and price guide", DISHWASHER_B_MD),
]


class StubGateway:
    """Stands in for get_gateway(): records calls and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, num: int):
        self.calls.append((query, num))
        return (
            [
                Result(
                    url=APOLLO_1202 if query == Q2 and i == 0 else f"https://provider.example.com/{i}",
                    title=f"stub result {i}",
                    snippet="stub",
                )
                for i in range(num)
            ],
            "stub",
            [],
        )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

async def _fresh_database() -> None:
    """Drop the test DB so every run starts from a fixed, empty corpus."""
    s = get_settings()
    admin = await psycopg.AsyncConnection.connect(
        s.conninfo(s.POSTGRES_ADMIN_DB), autocommit=True
    )
    try:
        async with admin.cursor() as cur:
            safe = s.POSTGRES_DB.replace('"', '""')
            await cur.execute(f'DROP DATABASE IF EXISTS "{safe}" WITH (FORCE)')
    finally:
        await admin.close()


async def _seed_dataset() -> None:
    """Store the fixed dataset except Apollo, which the provider warms later."""
    for url, title, md in DATASET:
        if url == APOLLO_1202:
            continue
        status, chunks = await store_page(url, md, title=title)
        assert status == "ok" and chunks >= 1, f"{url}: {status}, {chunks} chunks"
    print("OK dataset seeded:", len(DATASET) - 1, "pages")


async def _check_gate_columns() -> None:
    """fn_search_local exposes all three gate columns with the expected separation."""
    s = get_settings()
    qvec = await asyncio.to_thread(embed_one, Q1)
    rows = await db.fetch_all(
        "SELECT url, coverage, distinctive_coverage, similarity FROM fn_search_local(%s, %s::vector, 10)",
        (Q1, qvec),
    )
    by_url = {r["url"]: r for r in rows}
    dump = by_url[WORD_DUMP]
    article = by_url[CAR_SEAT_A]
    print("gate columns:", {u: (round(r["coverage"], 3), round(r["distinctive_coverage"] or -1, 3), round(r["similarity"] or -1, 3)) for u, r in by_url.items()})
    # the word dump contains every query word but is not topically similar
    assert dump["coverage"] >= 0.9, f"word dump coverage {dump['coverage']}"
    assert dump["similarity"] is not None and dump["similarity"] < s.LOCAL_MIN_SIMILARITY, (
        f"word dump similarity {dump['similarity']} should be below the gate"
    )
    # a real article clears both conditions
    assert article["coverage"] >= s.LOCAL_MIN_COVERAGE, f"article coverage {article['coverage']}"
    assert article["similarity"] is not None and article["similarity"] >= s.LOCAL_MIN_SIMILARITY, (
        f"article similarity {article['similarity']} should clear the gate"
    )
    # Q1 has no rare words in this small corpus, so the distinctive gate is vacuous
    # (distinctive_coverage 1.0) for every row — a wiring check; the brand-word
    # behavior itself is pinned by _check_distinctive_*.
    assert dump["distinctive_coverage"] == 1.0, f"word dump distinctive {dump['distinctive_coverage']}"
    assert article["distinctive_coverage"] == 1.0, f"article distinctive {article['distinctive_coverage']}"
    print("OK gate columns (coverage + similarity + distinctive separation)")


async def _check_local_hit_full_set(stub: StubGateway) -> None:
    """Q1 with k=2: both car-seat pages pass, so local serves and no provider is called."""
    calls_before = len(stub.calls)
    out = await sw.search_web(Q1, num_results=2)
    assert out["source"] == "local", f"expected local, got {out['source']}: {out}"
    urls = [r["url"] for r in out["results"]]
    assert set(urls) == {CAR_SEAT_A, CAR_SEAT_B}, urls
    assert stub.calls[calls_before:] == [], f"provider must not be called on a local hit: {stub.calls}"
    print("OK local hit (full passing set served, zero provider credits)")


async def _check_provider_warms_strong_partial(stub: StubGateway) -> None:
    """A provider miss enqueues Apollo; after indexing it, repeat Q2 serves local."""
    enqueued: list[str] = []

    async def record_enqueue(url: str, source: str = "search") -> bool:
        """Record the background job without crawling on the live network."""
        assert source == "search", source
        enqueued.append(url)
        return True

    original_enqueue = sw.queue.enqueue
    sw.queue.enqueue = record_enqueue
    try:
        first = await sw.search_web(Q2, num_results=5, max_crawl=5)
        assert first["source"] == "stub", f"first search must miss locally: {first}"
        assert stub.calls == [(Q2, 5)], stub.calls
        assert APOLLO_1202 in enqueued, f"provider URL must be queued for indexing: {enqueued}"

        status, chunks = await store_page(APOLLO_1202, APOLLO_1202_MD, title="Apollo 11 alarm")
        assert status == "ok" and chunks >= 1, f"provider URL not indexed: {status}, {chunks}"
        qvec = await asyncio.to_thread(embed_one, Q2)
        rows = await db.fetch_all(
            "SELECT url, coverage, similarity FROM fn_search_local(%s, %s::vector, 10)",
            (Q2, qvec),
        )
        apollo = next(r for r in rows if r["url"] == APOLLO_1202)
        s = get_settings()
        assert apollo["coverage"] >= s.LOCAL_MIN_COVERAGE, apollo
        assert apollo["similarity"] >= s.LOCAL_PARTIAL_MIN_SIMILARITY, apollo

        second = await sw.search_web(Q2, num_results=5)
        assert second["source"] == "local" and not second["degraded"], second
        assert [r["url"] for r in second["results"]] == [APOLLO_1202], second
        assert stub.calls == [(Q2, 5)], f"repeat search must spend no provider credit: {stub.calls}"
    finally:
        sw.queue.enqueue = original_enqueue
    print("OK provider miss -> indexed strong page -> repeat query served locally")


async def _check_marginal_partial_defers(stub: StubGateway) -> None:
    """One row at the ordinary gate's floor is not a confident local answer."""
    s = get_settings()
    similarity = (s.LOCAL_MIN_SIMILARITY + s.LOCAL_PARTIAL_MIN_SIMILARITY) / 2
    assert s.LOCAL_MIN_SIMILARITY < similarity < s.LOCAL_PARTIAL_MIN_SIMILARITY
    original_search = sw._search_local_index

    async def marginal_index(
        query: str,
        k: int,
        max_age_days: float | None,
    ) -> tuple[list[dict], int, str | None]:
        """Return one candidate that passes the ordinary gate but is marginal."""
        return [{"coverage": s.LOCAL_MIN_COVERAGE, "similarity": similarity,
                 "url": APOLLO_1202}], 0, None

    sw._search_local_index = marginal_index
    calls_before = len(stub.calls)
    try:
        out = await sw.search_web("marginal single result", num_results=5, max_crawl=0)
        assert out["source"] == "stub", out
        assert stub.calls[calls_before:] == [("marginal single result", 5)], stub.calls
    finally:
        sw._search_local_index = original_search
    print("OK marginal partial match defers to provider")


async def _check_multiple_partial_serves(stub: StubGateway) -> None:
    """Several ordinary matches can serve locally even when none is strong."""
    s = get_settings()
    assert s.LOCAL_PARTIAL_MIN_PASSING < 5
    similarity = (s.LOCAL_MIN_SIMILARITY + s.LOCAL_PARTIAL_MIN_SIMILARITY) / 2
    original_search = sw._search_local_index
    urls = [APOLLO_1202, CAR_SEAT_A, CAR_SEAT_B]

    async def moderate_index(
        query: str,
        k: int,
        max_age_days: float | None,
    ) -> tuple[list[dict], int, str | None]:
        """Return three separate pages passing the ordinary relevance gate."""
        return [
            {"coverage": s.LOCAL_MIN_COVERAGE, "similarity": similarity, "url": url}
            for url in urls
        ], 0, None

    sw._search_local_index = moderate_index
    calls_before = len(stub.calls)
    try:
        out = await sw.search_web("multiple moderate matches", num_results=5)
        assert out["source"] == "local" and not out["degraded"], out
        # All three stub rows share example.com, so the per-domain cap
        # (SEARCH_MAX_PER_DOMAIN) trims the third; equal similarity and score
        # keep the input order stable.
        expected = urls[: s.SEARCH_MAX_PER_DOMAIN]
        assert [r["url"] for r in out["results"]] == expected, out
        assert stub.calls[calls_before:] == [], f"partial hit must avoid provider: {stub.calls}"
    finally:
        sw._search_local_index = original_search
    print("OK multiple moderate matches serve a partial local set")


async def _check_distinctive_coverage_offbrand() -> None:
    """A page covering every common word but missing a rare brand word scores 0.0."""
    s = get_settings()
    # "playtex" is absent from the corpus (rare); newborn/car/seat are in the car-seat pages.
    qvec = await asyncio.to_thread(embed_one, "Playtex newborn car seat")
    rows = await db.fetch_all(
        "SELECT url, coverage, distinctive_coverage FROM fn_search_local(%s, %s::vector, 20)",
        ("Playtex newborn car seat", qvec),
    )
    by_url = {r["url"]: r for r in rows}
    assert CAR_SEAT_A in by_url, f"car-seat page should rank for the query: {list(by_url)}"
    row = by_url[CAR_SEAT_A]
    # covers the common words (would clear the old coverage gate) but has no brand word
    assert row["coverage"] >= s.LOCAL_MIN_COVERAGE, f"off-brand coverage {row['coverage']}"
    assert row["distinctive_coverage"] == 0.0, f"off-brand distinctive should be 0: {row['distinctive_coverage']}"
    print("OK distinctive coverage marks an off-brand page as missing the brand word")


async def _check_distinctive_gate_defers(stub: StubGateway) -> None:
    """Rows that clear coverage+similarity but miss every distinctive term defer."""
    original_search = sw._search_local_index

    async def offbrand_index(
        query: str,
        k: int,
        max_age_days: float | None,
    ) -> tuple[list[dict], int, str | None]:
        """Three pages cover the common words and are topically similar, but none
        contains the query's rare brand word (distinctive_coverage 0)."""
        return [
            {"coverage": 1.0, "similarity": 0.6, "distinctive_coverage": 0.0,
             "url": f"https://example.com/offbrand-{i}"}
            for i in range(3)
        ], 0, None

    sw._search_local_index = offbrand_index
    calls_before = len(stub.calls)
    try:
        out = await sw.search_web("Playtex baby bottles reviews", num_results=5, max_crawl=0)
        assert out["source"] == "stub", f"off-brand pages must defer to provider: {out}"
        assert stub.calls[calls_before:] == [("Playtex baby bottles reviews", 5)], stub.calls
    finally:
        sw._search_local_index = original_search
    print("OK distinctive gate defers an off-brand partial set (coverage+similarity pass, brand missing)")


async def _check_distinctive_gate_serves(stub: StubGateway) -> None:
    """A page covering every distinctive term serves; off-brand companions drop out."""
    s = get_settings()
    onbrand = "https://example.com/playtex-bottle-reviews"
    original_search = sw._search_local_index

    async def onbrand_index(
        query: str,
        k: int,
        max_age_days: float | None,
    ) -> tuple[list[dict], int, str | None]:
        """One page covers the common words AND the rare brand word; a second is off-brand."""
        return [
            {"coverage": 1.0, "similarity": s.LOCAL_PARTIAL_MIN_SIMILARITY + 0.05,
             "distinctive_coverage": 1.0, "url": onbrand},
            {"coverage": 1.0, "similarity": 0.6, "distinctive_coverage": 0.0,
             "url": "https://example.com/offbrand-0"},
        ], 0, None

    sw._search_local_index = onbrand_index
    calls_before = len(stub.calls)
    try:
        out = await sw.search_web("Playtex baby bottles reviews", num_results=5, max_crawl=0)
        assert out["source"] == "local" and not out["degraded"], out
        urls = [r["url"] for r in out["results"]]
        assert urls == [onbrand], f"only the on-brand page should serve: {urls}"
        assert stub.calls[calls_before:] == [], f"on-brand hit must avoid provider: {stub.calls}"
    finally:
        sw._search_local_index = original_search
    print("OK distinctive gate serves the on-brand page and drops off-brand companions")


async def _check_word_dump_never_serves(stub: StubGateway) -> None:
    """Q3: the word dump tops the index with coverage 1.0 but must not be served."""
    out = await sw.search_web(Q3, num_results=5)
    assert out["source"] == "stub", f"expected provider fallback, got {out['source']}: {out}"
    urls = [r["url"] for r in out["results"]]
    assert WORD_DUMP not in urls, "word dump must never be served locally"

    s = get_settings()
    qvec = await asyncio.to_thread(embed_one, Q3)
    rows = await db.fetch_all(
        "SELECT url, coverage, similarity FROM fn_search_local(%s, %s::vector, 10)",
        (Q3, qvec),
    )
    by_url = {r["url"]: r for r in rows}
    dump = by_url[WORD_DUMP]
    # the gate is what saved us: full coverage, failing similarity
    assert dump["coverage"] >= 0.9, f"word dump coverage {dump['coverage']}"
    assert dump["similarity"] is not None and dump["similarity"] < s.LOCAL_MIN_SIMILARITY, (
        f"word dump similarity {dump['similarity']} should be below the gate"
    )
    print("OK word dump never served (coverage 1.0, similarity below gate)")


async def _check_catalog_reaches_coverage_floor(stub: StubGateway) -> None:
    """Q4 with k=2: one catalog covers 2/4 lexemes, the price guide 3/4.
    Only one cleared the old 0.75 bar; at 0.5 both serve without a provider.
    Mentioning 700 does not establish that a product costs less than $700."""
    qvec = await asyncio.to_thread(embed_one, Q4)
    rows = await db.fetch_all(
        "SELECT url, coverage FROM fn_search_local(%s, %s::vector, 10)",
        (Q4, qvec),
    )
    coverage = {r["url"]: r["coverage"] for r in rows}
    assert coverage[DISHWASHER_A] == 0.5, coverage
    assert coverage[DISHWASHER_B] == 0.75, coverage

    before = len(stub.calls)  # earlier checks legitimately call the provider
    out = await sw.search_web(Q4, num_results=2)
    assert out["source"] == "local", f"expected local, got {out['source']}: {out}"
    urls = [r["url"] for r in out["results"]]
    assert set(urls) == {DISHWASHER_A, DISHWASHER_B}, urls
    assert stub.calls[before:] == [], f"provider must not be called on a local hit: {stub.calls[before:]}"
    print("OK catalog pages clear the coverage floor (0.5 and 0.75)")


async def _check_local_mode_bypasses_gate() -> None:
    """search_mode='local' serves index rows even when the gate would not pass."""
    out = await sw.search_web(Q2, num_results=5, search_mode="local")
    assert out["source"] == "local", f"expected local, got {out['source']}: {out}"
    urls = [r["url"] for r in out["results"]]
    assert APOLLO_1202 in urls, urls
    print("OK local mode bypasses the gate")


async def main() -> None:
    """Run the search regression suite against a fresh throwaway database."""
    await _fresh_database()
    await db.startup()
    print("OK startup (test DB rebuilt, schema applied)")
    await _seed_dataset()

    stub = StubGateway()
    sw.get_gateway = lambda: stub  # no network: the gateway is canned
    try:
        await _check_provider_warms_strong_partial(stub)
        await _check_gate_columns()
        await _check_distinctive_coverage_offbrand()
        await _check_local_hit_full_set(stub)
        await _check_marginal_partial_defers(stub)
        await _check_multiple_partial_serves(stub)
        await _check_distinctive_gate_defers(stub)
        await _check_distinctive_gate_serves(stub)
        await _check_word_dump_never_serves(stub)
        await _check_catalog_reaches_coverage_floor(stub)
        await _check_local_mode_bypasses_gate()
    finally:
        await db.close()
    print("ALL SEARCH REGRESSION TESTS PASSED")


asyncio.run(main())
