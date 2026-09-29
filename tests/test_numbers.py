import random
from decimal import Decimal
from fractions import Fraction

import pytest

from glassvault.bank import seed_bank
from glassvault.numbers import (
    AmountMention,
    FractionMention,
    ambiguous_amounts,
    asr_confusables,
    confusable_reasons,
    extract_amounts,
    extract_fractions,
    mention_confusables,
    parse_amount,
)


def amounts(text: str) -> list[tuple[str, str]]:
    """(value, exact words) for each mention, so a table row reads like the transcript."""
    return [(str(m.value), m.text) for m in extract_amounts(text)]


def fractions(text: str) -> list[tuple[int, int, str]]:
    return [(f.numerator, f.denominator, f.text) for f in extract_fractions(text)]


# --- What counts as money ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # The brief's number forms
        ("a grand", [("1000.00", "a grand")]),
        ("two grand", [("2000.00", "two grand")]),
        ("5 grand", [("5000.00", "5 grand")]),
        ("5k", [("5000.00", "5k")]),
        ("5K", [("5000.00", "5K")]),
        ("5 k", [("5000.00", "5 k")]),  # Singlish spacing
        ("1.2k", [("1200.00", "1.2k")]),
        ("2m", [("2000000.00", "2m")]),
        ("1.5 million", [("1500000.00", "1.5 million")]),
        ("2 mil", [("2000000.00", "2 mil")]),
        ("5 hundred", [("500.00", "5 hundred")]),
        ("S$1,200.50", [("1200.50", "S$1,200.50")]),
        ("$50", [("50.00", "$50")]),
        ("$ 50", [("50.00", "$ 50")]),
        ("SGD 50", [("50.00", "SGD 50")]),
        ("SGD50", [("50.00", "SGD50")]),
        ("sgd 1,250.50", [("1250.50", "sgd 1,250.50")]),
        ("SG$75", [("75.00", "SG$75")]),
        ("50sgd", [("50.00", "50sgd")]),
        ("50 SGD", [("50.00", "50 SGD")]),
        ("$50k", [("50000.00", "$50k")]),
        ("1,200", [("1200.00", "1,200")]),
        ("1,000,000", [("1000000.00", "1,000,000")]),
        ("12.5", [("12.50", "12.5")]),
        ("0.50", [("0.50", "0.50")]),
        ("50 bucks", [("50.00", "50 bucks")]),
        ("50 dollar", [("50.00", "50 dollar")]),  # Singlish singular
        ("50 Singapore dollars", [("50.00", "50 Singapore dollars")]),
        ("fifty dollars", [("50.00", "fifty dollars")]),
        ("fifty bucks", [("50.00", "fifty bucks")]),
        ("fifty", [("50.00", "fifty")]),
        ("twenty-five", [("25.00", "twenty-five")]),
        ("fifty five", [("55.00", "fifty five")]),
        ("two hundred and fifty", [("250.00", "two hundred and fifty")]),
        ("two hundred fifty", [("250.00", "two hundred fifty")]),
        ("one thousand five hundred", [("1500.00", "one thousand five hundred")]),
        ("fifteen hundred", [("1500.00", "fifteen hundred")]),
        ("twenty five hundred", [("2500.00", "twenty five hundred")]),
        ("a hundred", [("100.00", "a hundred")]),
        ("a hundred and fifty", [("150.00", "a hundred and fifty")]),
        ("a thousand", [("1000.00", "a thousand")]),
        ("a million", [("1000000.00", "a million")]),
        ("five hundred thousand", [("500000.00", "five hundred thousand")]),
        ("one million two hundred thousand", [("1200000.00", "one million two hundred thousand")]),
        ("two thousand and twenty six", [("2026.00", "two thousand and twenty six")]),
        ("two k", [("2000.00", "two k")]),
        ("two-k", [("2000.00", "two-k")]),
        ("fifty k", [("50000.00", "fifty k")]),
        ("half a grand", [("500.00", "half a grand")]),
        ("half a million", [("500000.00", "half a million")]),
        ("one and a half k", [("1500.00", "one and a half k")]),
        ("two and a half thousand", [("2500.00", "two and a half thousand")]),
        ("two and a half dollars", [("2.50", "two and a half dollars")]),
        ("one and half k", [("1500.00", "one and half k")]),  # Singlish drops the "a"
        ("one point five k", [("1500.00", "one point five k")]),
        ("twelve point five", [("12.50", "twelve point five")]),
        ("fourty dollars", [("40.00", "fourty dollars")]),  # common misspelling
        ("FIFTY DOLLARS", [("50.00", "FIFTY DOLLARS")]),
        # Cents
        ("50 cents", [("0.50", "50 cents")]),
        ("fifty cents", [("0.50", "fifty cents")]),
        ("fifty dollars and twenty cents", [("50.20", "fifty dollars and twenty cents")]),
        ("5 dollars 5 cents", [("5.05", "5 dollars 5 cents")]),
        ("twelve dollars fifty", [("12.50", "twelve dollars fifty")]),
        ("fifty eight dollars ninety", [("58.90", "fifty eight dollars ninety")]),
        ("a dollar fifty", [("1.50", "a dollar fifty")]),
        ("one dollar", [("1.00", "one dollar")]),
        # Bare "hundred"/"thousand" (Singlish drops the article)
        ("send mum thousand", [("1000.00", "thousand")]),
        # Round thousands in the year range are still money
        ("send mum 2000", [("2000.00", "2000")]),
        ("send mum 1950", [("1950.00", "1950")]),
        ("S$2026", [("2026.00", "S$2026")]),
        ("2026 dollars", [("2026.00", "2026 dollars")]),
        # Currency beats the phone rule
        ("$81234567", [("81234567.00", "$81234567")]),
    ],
)
def test_amount_forms(text, expected):
    assert amounts(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Share counts and other counted things
        "12 shares",
        "buy 12 DBS shares",
        "buy 12 more DBS shares",
        "two hundred shares",
        "a hundred and twenty units",
        "5k shares",
        "sell 100 of my DBS shares",
        "3 days", "for 12 months", "3 people", "2 bills", "the last 4 digits",
        "5 km", "2 m",  # a spaced "m" is metres, not million
        # Account, card, phone and reference numbers
        "account 93-1182-44",
        "ACC 93-1182-44",
        "Mobile a/c 8817-2201",
        "account 12345678",
        "account no. 1234567",
        "+65 8123 4567",
        "8123 4567",
        "81234567",
        "9123-4567",
        "call me at 91234567",
        "tel: 6123 4567",
        "ending 4521",
        "the one ending 4521",
        "card ending in 8892",
        "ends with 1107",
        "last four digits 4521",
        "+65 ••••4521",
        "****4521",
        "ref 12345",
        "invoice 889",
        "option 2",
        "pin 1234",
        "OTP 123456",
        "postal code 123456",
        "blk 123",
        "#05-12",
        "card no. 5555",
        "four five two one",
        "nine one one",
        # Tokens glued to letters: step ids, SGX symbols, NRIC, emails
        "t1", "D05", "O39 and Z74", "S1234567A", "john50@example.com", "<REF_1>",
        # Times
        "3pm", "3 pm", "3 p.m.", "10am", "3:30", "15:30", "3.30pm", "three pm", "three o'clock",
        "half past three", "1530hrs",
        # Dates and years
        "2026", "in 2026", "since 2000", "Sep 2026", "September 2026", "FY2026",
        "25 Sep", "Sep 25", "on 25th", "the 5th", "2nd", "25/9", "25/09/2026", "2026-09-25",
        "on the twenty fifth", "the twenty-first", "on May 20", "on 20 June",
        "twenty twenty-six", "nineteen ninety-nine",
        # Fractions and percentages are not amounts
        "30%", "30 %", "thirty percent", "50 per cent", "half", "half of it", "a quarter",
        "a third", "one third", "two thirds", "three quarters", "fifty-fifty", "50/50",
        # Vague or ranged amounts
        "a few hundred", "a couple hundred", "couple of thousand", "several thousand",
        "hundreds", "thousands of dollars", "500+", "500-ish", "500ish", "50-100", "5 - 10k",
        "send John a hundred plus",
        # Foreign currency
        "US$50", "USD 50", "50 USD", "300 US dollars", "RM50", "50 ringgit", "€50", "£50",
        "¥5000", "50 euros", "HK$100", "100 Hong Kong dollars", "A$20",
        # Not numbers at all, or not positive
        "zero", "0", "0 dollars", "12.345", "1.2.3", "007", "12,00",
        # Pronoun "one", mixed numbers
        "the joint one", "one of them", "two and a half years",
        # Idioms
        "a grand total of", "the Grand Hyatt",
    ],
)
def test_not_money(text):
    assert extract_amounts(text) == []


def test_send_john_fifty():
    m = extract_amounts("send John fifty")
    assert m == [AmountMention(Decimal("50.00"), "fifty", 10, 15)]
    assert parse_amount("send John fifty") == Decimal("50")


def test_spans_are_exact_and_values_are_cents():
    text = "Move a grand to joint, pay S$1,200.50 to mum, send John fifty bucks and 5k to savings."
    found = extract_amounts(text)
    assert [(m.text, str(m.value)) for m in found] == [
        ("a grand", "1000.00"),
        ("S$1,200.50", "1200.50"),
        ("fifty bucks", "50.00"),
        ("5k", "5000.00"),
    ]
    for m in found:
        assert text[m.start : m.end] == m.text
        assert m.value.as_tuple().exponent == -2


def test_brief_sentence():
    text = "Move a grand to my joint account, buy DBS shares with half of it, pay my SP bill, and send John fifty."
    assert amounts(text) == [("1000.00", "a grand"), ("50.00", "fifty")]
    assert fractions(text) == [(1, 2, "half")]


def test_singlish_request():
    text = ("Eh, send John fifty for the makan, then transfer two k to my wife's POSB, "
            "and put half my bonus in DBS shares lah")
    assert amounts(text) == [("50.00", "fifty"), ("2000.00", "two k")]
    assert fractions(text) == [(1, 2, "half")]


def test_count_nouns_do_not_swallow_money():
    # "for" ends the look-ahead, so "tickets" does not make 40 a count.
    assert amounts("Send John Lim 40 for the concert tickets") == [("40.00", "40")]
    assert amounts("Send mum 500 every month") == [("500.00", "500")]
    assert amounts("Buy DBS shares with 500") == [("500.00", "500")]
    assert amounts("Buy 1000 dollars of DBS and 500 of OCBC") == [("1000.00", "1000 dollars"), ("500.00", "500")]
    assert amounts("put 500 into DBS shares") == [("500.00", "500")]
    assert amounts("buy $500 of DBS shares") == [("500.00", "$500")]
    assert amounts("send mum 500 two days later") == [("500.00", "500")]
    assert amounts("Pay John 1,500.00 for the 3 nights") == [("1500.00", "1,500.00")]
    assert amounts("split 90 dollars three ways") == [("90.00", "90 dollars")]
    assert amounts("send 500 plus the SP bill") == [("500.00", "500")]
    assert amounts("buy 12 DBS shares at $40.12") == [("40.12", "$40.12")]
    # Only a small whole number can be a fraction's numerator ("two thirds").
    assert amounts("pay 1,200.50 half from joint") == [("1200.50", "1,200.50")]


def test_labels_only_bind_the_number_right_after_them():
    assert amounts("from my savings account 500 to joint") == [("500.00", "500")]
    assert amounts("top up my ez-link card 50") == [("50.00", "50")]
    assert amounts("pay my SP bill 142.35") == [("142.35", "142.35")]
    assert amounts("No, fifty.") == [("50.00", "fifty")]
    assert amounts("the one ending 4521, send him 30") == [("30.00", "30")]


def test_months_that_are_also_names():
    # "May" and "June" are first names in Singapore; only a date preposition makes them dates.
    assert amounts("send May 20") == [("20.00", "20")]
    assert amounts("send 20 to June") == [("20.00", "20")]
    assert amounts("pay on May 20") == []
    assert amounts("send 20 Oct") == []


def test_list_of_amounts_in_order():
    assert amounts("fifty and twenty") == [("50.00", "fifty"), ("20.00", "twenty")]
    assert amounts("make it 80 not 50") == [("80.00", "80"), ("50.00", "50")]
    assert amounts("fifty dollars and twenty to Mary") == [("50.00", "fifty dollars"), ("20.00", "twenty")]
    assert amounts("50 dollars 20 to Mary") == [("50.00", "50 dollars"), ("20.00", "20")]


def test_poisoned_biller_reference():
    # The grammar is honest about untrusted text too: 9000 IS an amount there. Keeping
    # it out of prompts is context.py's job; the phone and account numbers are never money.
    reference = seed_bank(__import__("datetime").date(2026, 10, 2)).billers["SP_GROUP"].reference
    assert amounts(reference) == [("9000.00", "9000")]


# --- Two honest readings ----------------------------------------------------------


@pytest.mark.parametrize(
    "text, readings",
    [
        ("two fifty", ["250.00", "2.50"]),
        ("two-fifty", ["250.00", "2.50"]),
        ("one fifty", ["150.00", "1.50"]),
        ("twelve fifty", ["1250.00", "12.50"]),
        ("four ninety-nine", ["499.00", "4.99"]),
        ("twenty five fifty", ["2550.00", "25.50"]),
    ],
)
def test_paired_words_return_both_readings_on_one_span(text, readings):
    found = extract_amounts(text)
    assert [str(m.value) for m in found] == readings
    assert {(m.start, m.end, m.text) for m in found} == {(0, len(text), text)}
    assert parse_amount(text) is None  # the caller must ask
    assert [len(g) for g in ambiguous_amounts(text)] == [2]


@pytest.mark.parametrize(
    "text, value",
    [
        ("two fifty dollars", "250.00"),  # nobody says that for S$2.50
        ("two dollars fifty", "2.50"),
        ("two fifty cents", "2.50"),
        ("two fifty k", "250000.00"),
    ],
)
def test_units_settle_a_paired_reading(text, value):
    assert [str(m.value) for m in extract_amounts(text)] == [value]
    assert ambiguous_amounts(text) == []


# --- parse_amount -------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase, value",
    [
        ("a grand", "1000"),
        ("fifty", "50"),
        ("S$1,200.50", "1200.50"),
        ("about 500", "500"),
        ("fifty, yes fifty", "50"),
        ("half of it", None),
        ("12 shares", None),
        ("2026", None),
        ("fifty or sixty", None),
        ("", None),
        ("John Tan", None),
    ],
)
def test_parse_amount(phrase, value):
    assert parse_amount(phrase) == (Decimal(value) if value else None)


# --- Fractions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("half of it", [(1, 2, "half")]),
        ("half", [(1, 2, "half")]),
        ("put half my bonus in DBS", [(1, 2, "half")]),
        ("a half of it", [(1, 2, "a half")]),
        ("one half", [(1, 2, "one half")]),
        ("a quarter", [(1, 4, "a quarter")]),
        ("a quarter of that", [(1, 4, "a quarter")]),
        ("quarter of my savings", [(1, 4, "quarter")]),
        ("three quarters", [(3, 4, "three quarters")]),
        ("a third", [(1, 3, "a third")]),
        ("one third of it", [(1, 3, "one third")]),
        ("two thirds", [(2, 3, "two thirds")]),
        ("a tenth of it", [(1, 10, "a tenth")]),
        ("two fifths", [(2, 5, "two fifths")]),
        ("30%", [(30, 100, "30%")]),
        ("30 %", [(30, 100, "30 %")]),
        ("30% of it", [(30, 100, "30%")]),
        ("thirty percent", [(30, 100, "thirty percent")]),
        ("fifty per cent", [(50, 100, "fifty per cent")]),
        ("a hundred percent", [(100, 100, "a hundred percent")]),
        ("12.5%", [(125, 1000, "12.5%")]),
        ("fifty-fifty", [(1, 2, "fifty-fifty")]),
        ("split it half-half", [(1, 2, "half-half")]),
        ("50/50", [(1, 2, "50/50")]),
        ("1/2 of it", [(1, 2, "1/2")]),
        ("3/4 of the bonus", [(3, 4, "3/4")]),
        # "and" before a fraction only makes a mixed number after a number
        ("move 1000 to joint and half to shares", [(1, 2, "half")]),
        ("pay SP and a third to mum", [(1, 3, "a third")]),
    ],
)
def test_fraction_forms(text, expected):
    assert fractions(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "half a grand",  # an amount, not a fraction
        "one and a half k",
        "two and a half years",
        "two and half years",
        "half past three",
        "half an hour",
        "a quarter past three",
        "last quarter of the year",
        "the third of October",
        "a third party",
        "a fifth time",
        "on 1/2",
        "25/9",
        "0%",
        "the joint one",
        "12 shares",
    ],
)
def test_not_fractions(text):
    assert extract_fractions(text) == []


def test_fractions_are_reported_as_said():
    # The drafter writes "30% of it" as 30/100, so the provenance check compares like with like.
    f = extract_fractions("send mum 50% of it")[0]
    assert (f.numerator, f.denominator) == (50, 100)
    assert f.value == Fraction(1, 2) == extract_fractions("half")[0].value


def test_fraction_spans_are_exact():
    text = "Move 5k to joint and send mum a tenth of it, then 30% to shares"
    found = extract_fractions(text)
    assert [(f.numerator, f.denominator) for f in found] == [(1, 10), (30, 100)]
    for f in found:
        assert text[f.start : f.end] == f.text
    assert isinstance(found[0], FractionMention)


# --- ASR confusables ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("50", {"15"}),  # the contract's own examples, exactly
        ("15", {"50"}),
        ("5000", {"500", "50000"}),
        ("13", {"30"}),
        ("90", {"19"}),
        ("115", {"150"}),
        ("150", {"115"}),
        ("500", {"5000"}),
        ("15000", {"50000", "1500", "150000"}),
        ("15.50", {"50.50", "15.15"}),
        ("0.15", {"0.50"}),
        ("55", set()),  # "fifteen five" is not a number
        ("32", set()),
        ("12", set()),
        ("1", set()),
        ("1234", set()),
    ],
)
def test_asr_confusables(value, expected):
    assert asr_confusables(Decimal(value)) == {Decimal(v) for v in expected}


@pytest.mark.parametrize("teen, ty", [(13, 30), (14, 40), (15, 50), (16, 60), (17, 70), (18, 80), (19, 90)])
def test_every_teen_ty_pair(teen, ty):
    assert Decimal(ty) in asr_confusables(Decimal(teen))
    assert Decimal(teen) in asr_confusables(Decimal(ty))
    assert Decimal(ty * 1000) in asr_confusables(Decimal(teen * 1000))


def test_confusables_are_symmetric_and_never_include_the_value():
    rng = random.Random(7)
    values = [Decimal(n) for n in range(1, 2001)]
    values += [Decimal(rng.randint(1, 9_999_999)) / 100 for _ in range(1500)]
    values += [Decimal(n * 100) for n in range(1, 1001)]
    for v in values:
        found = asr_confusables(v)
        assert v not in found
        for c in found:
            assert c > 0 and c == c.quantize(Decimal("0.01"))
            assert v in asr_confusables(c), (v, c)


def test_confusable_reasons():
    assert confusable_reasons(Decimal("5000")) == {Decimal("500"): "magnitude", Decimal("50000"): "magnitude"}
    assert confusable_reasons(Decimal("50")) == {Decimal("15"): "teen_ty"}


@pytest.mark.parametrize("bad", ["0", "-50", "12.345", "NaN"])
def test_confusables_of_non_amounts_are_empty(bad):
    assert asr_confusables(Decimal(bad)) == set()


def test_mention_confusables_add_the_lost_thousand_only_for_bare_amounts():
    bare = extract_amounts("send John fifty")[0]
    assert bare.bare
    assert mention_confusables(bare) == {Decimal("15"), Decimal("50000")}

    for said in ("send John fifty dollars", "send John $50", "send John 5k", "send John five hundred"):
        m = extract_amounts(said)[0]
        assert not m.bare
        assert mention_confusables(m) == asr_confusables(m.value)


# --- Properties ---------------------------------------------------------------------

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen \
sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def spell(n: int, *, british: bool) -> str:
    """Independent oracle: how people write n in words.

    British style adds "and" before the last two digits: "one hundred and five",
    "one thousand and five".
    """
    parts = []
    total = n
    for scale, name in ((10**6, "million"), (1000, "thousand"), (1, "")):
        group, n = divmod(n, scale)
        if not group:
            continue
        hundreds, rest = divmod(group, 100)
        words = [f"{_ONES[hundreds]} hundred"] if hundreds else []
        if rest:
            if british and (hundreds or (scale == 1 and total >= 1000)):
                words.append("and")
            tens, ones = divmod(rest, 10)
            words.append(_ONES[rest] if rest < 20 else _TENS[tens] + (f"-{_ONES[ones]}" if ones else ""))
        parts.append(" ".join(words) + (f" {name}" if name else ""))
    return " ".join(parts)


def test_every_amount_written_in_words_reads_back():
    rng = random.Random(11)
    sample = list(range(1, 1100)) + [rng.randint(1100, 9_999_999) for _ in range(2000)]
    for n in sample:
        for british in (False, True):
            words = spell(n, british=british) + " dollars"
            assert parse_amount(words) == Decimal(n), words


def test_every_amount_written_in_digits_reads_back():
    rng = random.Random(13)
    for _ in range(2000):
        cents = rng.randint(1, 10**9)
        value = Decimal(cents) / 100
        for text in (f"S${value:,.2f}", f"${value}", f"{value:,.2f} dollars", f"SGD {value:,.2f}"):
            assert parse_amount(text) == value, text


def test_output_invariants_on_noisy_text():
    rng = random.Random(5)
    vocab = ("send pay John mum fifty fifteen a grand k 5k 1.2k $ S$ 1,200.50 2026 3pm +65 8123 4567 "
             "shares 12 half of it 30% two hundred and dollars cents ending 4521 account 93-1182-44 , . - "
             "thousand one point five quarter third twelve ninety-nine or to for").split()
    for _ in range(2000):
        text = " ".join(rng.choice(vocab) for _ in range(rng.randint(1, 14)))
        for m in extract_amounts(text):
            assert text[m.start : m.end] == m.text
            assert m.value > 0 and m.value == m.value.quantize(Decimal("0.01"))
        for f in extract_fractions(text):
            assert text[f.start : f.end] == f.text
            assert f.numerator >= 1 and f.denominator >= 1
        # Fractions never overlap an amount's words.
        spans = [(m.start, m.end) for m in extract_amounts(text)]
        for f in extract_fractions(text):
            assert not any(s < f.end and f.start < e for s, e in spans), text


@pytest.mark.parametrize("text, value", [
    ("Buy 13000 of DBS shares.", "13000.00"),
    ("Buy 2000 of OCBC shares from the joint account.", "2000.00"),
    ("buy 500 of Singtel", "500.00"),
])
def test_n_of_a_named_stock_is_cash(text, value):
    assert [str(m.value) for m in extract_amounts(text)] == [value]


@pytest.mark.parametrize("text", ["sell 100 of my DBS shares", "two of my accounts", "send 3 of the bills"])
def test_n_of_a_determiner_stays_a_count(text):
    assert extract_amounts(text) == []
