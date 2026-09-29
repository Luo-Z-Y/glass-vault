"""Deterministic amount grammar for spoken and typed Singapore English.

The LLM never decides what number the customer said; this module does, with
rules small enough to read and test:

- Provenance: ``extract_amounts`` lists every S$ amount in a transcript with
  its exact span, so the control plane can check that each fixed amount in a
  draft (S$1,000) is something the customer actually said ("a grand").
- Relative amounts: ``extract_fractions`` finds "half", "a quarter", "30%", so
  a draft's ``FractionOfStep`` is checked the same way.
- Ambiguity: ``asr_confusables`` lists what a heard amount could really have
  been (fifteen/fifty, 500/5,000/50,000), so the agent can ask instead of guess.

Design rules, most important first:

1. When in doubt, a number is not money. Missing an amount costs one
   clarifying question; misreading a phone number, account number or year as
   money would let a draft cite it as "what the customer said". So identifiers,
   dates, times, share counts, foreign currency, ranges and vague amounts
   ("a few hundred", "500+") are all dropped.
2. Never round. "12.345" is not an S$ amount, so it is dropped, not rounded.
3. Never pick between honest readings. "two fifty" is S$250 or S$2.50, so both
   are returned on the same span and the caller asks.

Mandarin and Malay numerals (两万五, lima ratus) are a later extension.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import takewhile
from typing import NamedTuple

CENT = Decimal("0.01")


# --- Public types --------------------------------------------------------------


@dataclass(frozen=True)
class AmountMention:
    """An S$ amount stated in the text, and exactly where."""

    value: Decimal  # S$, 2 d.p.
    text: str  # the exact words, e.g. "a grand"; always source[start:end]
    start: int
    end: int

    @property
    def bare(self) -> bool:
        """Said with no currency and no scale word ("fifty", "500").

        Only a bare amount can have lost a trailing "thousand" or "k" in speech.
        """
        return not {t.low for t in _tokenize(self.text)} & _UNIT_MARKERS


@dataclass(frozen=True)
class FractionMention:
    """A share of an earlier amount, as said: "30%" is 30/100, "half" is 1/2.

    Not reduced, so it compares equal to a draft that writes the fraction the
    way the customer said it. Use ``value`` to compare regardless of form.
    """

    numerator: int
    denominator: int
    text: str
    start: int
    end: int

    @property
    def value(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


def extract_amounts(text: str) -> list[AmountMention]:
    """Every S$ amount in `text`, in order.

    Alternative readings of the same words ("two fifty": S$250 and S$2.50) are
    returned as adjacent mentions with the same span.
    """
    t = _Text(text)
    found: list[AmountMention] = []
    i = 0
    while i < t.n:
        read = _read_amount(t, i)
        if read is None:
            i += 1
            continue
        end, values = read
        start, stop = t.toks[i].start, t.toks[end - 1].end
        found.extend(AmountMention(v, text[start:stop], start, stop) for v in values)
        i = end
    return found


def parse_amount(phrase: str) -> Decimal | None:
    """The S$ amount in `phrase`, or None if it has none, several, or two readings.

    "a grand" -> 1000, "send John fifty" -> 50, "two fifty" -> None (ask).
    """
    values = {m.value for m in extract_amounts(phrase)}
    return values.pop() if len(values) == 1 else None


def extract_fractions(text: str) -> list[FractionMention]:
    """Every fraction of an amount in `text`: "half", "a quarter", "two thirds", "30%".

    Words inside an amount ("half a grand", "one and a half k") are not fractions.
    """
    t = _Text(text)
    amounts = [(m.start, m.end) for m in extract_amounts(text)]
    found: list[FractionMention] = []
    i = 0
    while i < t.n:
        hit = _read_fraction(t, i)
        if hit is None:
            i += 1
            continue
        numerator, denominator, start, end = hit
        if not any(s < end and start < e for s, e in amounts):
            found.append(FractionMention(numerator, denominator, text[start:end], start, end))
        while i < t.n and t.toks[i].start < end:
            i += 1
    return found


def asr_confusables(value: Decimal) -> set[Decimal]:
    """Amounts that speech-to-text or a slip of the finger could have turned into `value`.

    50 -> {15}, 15 -> {50}, 5000 -> {500, 50000}. Symmetric: b is in
    asr_confusables(a) exactly when a is in asr_confusables(b).
    """
    return set(confusable_reasons(value))


def confusable_reasons(value: Decimal) -> dict[Decimal, str]:
    """Like ``asr_confusables``, with why each one is confusable.

    - "teen_ty": one "-teen" heard as "-ty" or back (fifteen/fifty) anywhere in
      the spoken form, dollars or cents: 115 <-> 150, 15,000 <-> 50,000, 15.50 <-> 50.50.
    - "magnitude": a zero added or dropped, or "hundred" heard as "thousand".
      Only between round amounts that are both said with a scale word
      (500 <-> 5,000 <-> 50,000). "Fifty" has no scale word to slip, so 50 and
      500 are not confusable.
    """
    v = _exact_money(Decimal(value))
    if v is None:
        return {}
    dollars = int(v)
    cents = int((v - dollars) * 100)
    found: dict[Decimal, str] = {}
    for d in _teen_ty_swaps(dollars):
        found[Decimal(d) + Decimal(cents) / 100] = "teen_ty"
    for c in _teen_ty_swaps(cents):
        found[Decimal(dollars) + Decimal(c) / 100] = "teen_ty"
    if cents == 0 and dollars % 100 == 0:
        found.setdefault(v * 10, "magnitude")
    if cents == 0 and dollars % 1000 == 0:
        found.setdefault(v / 10, "magnitude")
    return {k.quantize(CENT): why for k, why in found.items() if k > 0 and k != v}


def mention_confusables(mention: AmountMention) -> set[Decimal]:
    """``asr_confusables`` plus, for a bare amount, the same number with "thousand" lost.

    "send John fifty" -> {15, 50000}: the brief's "fifty or fifty thousand?".
    "fifty dollars" -> {15}: the currency word shows no scale word was lost.
    Whether to actually ask is the caller's call (payee history, policy).
    """
    found = asr_confusables(mention.value)
    if mention.bare and mention.value < 1000 and mention.value == mention.value.to_integral_value():
        found.add((mention.value * 1000).quantize(CENT))
    return found


def ambiguous_amounts(text: str) -> list[tuple[AmountMention, ...]]:
    """Spans with more than one honest reading, e.g. ("two fifty" = 250, "two fifty" = 2.50)."""
    groups: dict[tuple[int, int], list[AmountMention]] = {}
    for m in extract_amounts(text):
        groups.setdefault((m.start, m.end), []).append(m)
    return [tuple(g) for g in groups.values() if len(g) > 1]


# --- Lexicon -------------------------------------------------------------------

_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SMALL = {**_UNITS, **_TEENS, **_TENS}
_BIG = {
    "thousand": 1000, "grand": 1000, "k": 1000,
    "million": 10**6, "mil": 10**6, "mn": 10**6,
    "billion": 10**9, "bn": 10**9,
}
_WORD_SCALES = {**_BIG, "hundred": 100}
# After digits: "5 hundred", "1.2k", "2m". A spaced "2 m" is metres, so "m" must be attached.
_DIGIT_SCALES = {**_WORD_SCALES, "m": 10**6}
# Scale words nobody uses for share counts or dates: they make a number explicit money.
_MONEY_SCALES = frozenset({"grand", "k", "m", "mil", "mn", "bn"})
_DIGIT_WORDS = {"zero": 0, "oh": 0, **_UNITS}
# Adjacent to another number, these mean digits read out one by one ("four five two one").
_RUN_WORDS = frozenset({*_SMALL, "zero"})

_DOLLAR_WORDS = frozenset({"dollar", "dollars", "buck", "bucks"})
_SGD_QUALIFIERS = frozenset({"singapore", "sing", "sg"})
_FOREIGN_QUALIFIERS = frozenset({
    "us", "usd", "american", "hk", "hongkong", "aussie", "australian", "au", "nz",
    "canadian", "taiwan", "taiwanese", "brunei",
})
_FOREIGN_WORDS = frozenset({
    "usd", "ringgit", "rm", "myr", "yuan", "rmb", "renminbi", "cny", "euro", "euros", "eur",
    "pound", "pounds", "gbp", "yen", "jpy", "baht", "thb", "rupiah", "idr", "rupee", "rupees",
    "inr", "won", "krw", "peso", "pesos", "php", "hkd", "aud", "nzd", "cad", "twd", "ntd", "bnd",
    "dong", "vnd", "chf", "franc", "francs", "dirham", "dirhams", "aed",
})
_FOREIGN_CODES = frozenset({  # before the number: "USD 50", "RM50"
    "usd", "rm", "myr", "eur", "gbp", "jpy", "cny", "rmb", "idr", "rp", "thb", "hkd", "aud",
    "inr", "rs", "krw", "php", "vnd", "twd", "ntd", "nzd", "cad", "chf", "bnd", "aed",
})
_FOREIGN_DOLLAR_PREFIXES = frozenset({"us", "hk", "a", "au", "nz", "c", "ca", "nt", "b"})  # "US$", "HK$"
_FOREIGN_SYMBOLS = frozenset("€£¥₹฿₱₫")
_UNIT_MARKERS = frozenset({"$", "sgd", "cent", "cents", *_DOLLAR_WORDS, *_DIGIT_SCALES})

# Words after a number that make it a count, not money: "12 shares", "3 days".
_COUNT_NOUNS = frozenset("""
    share shares unit units lot lots stock stocks
    people person persons pax friends guys kids children
    times day days week weeks wk wks month months mth mths year years yr yrs
    hour hours hr hrs minute minutes min mins second seconds sec secs
    item items piece pieces pc pcs ticket tickets night nights ways meals rounds sets pairs rooms seats
    accounts bills payments transfers transactions instalments installments digits
    km kg g cm mm ml l m gb mb kb sqft sqm psf x
""".split())
# Words that end the look-ahead for a count noun: "send 40 for the concert tickets" is money.
_BREAKERS = frozenset("""
    to into onto in for with worth from at per on and or but then via by as a an
    every each this next last is are was were so if because please pls plz only also too
    lah la leh lor ah hor meh can
    buy sell send pay transfer move put use give top invest
""".split())

_DENOMINATORS = {
    "half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4,
    "fourth": 4, "fourths": 4, "fifth": 5, "fifths": 5, "sixth": 6, "sixths": 6,
    "seventh": 7, "sevenths": 7, "eighth": 8, "eighths": 8, "ninth": 9, "ninths": 9,
    "tenth": 10, "tenths": 10,
}
# After "a third" / "one quarter": a clause end or one of these, else it is an
# ordinal ("a third party", "a fifth time").
_FRACTION_FOLLOWERS = frozenset("""
    of in into to for with and then on from my your our his her their the that this it
    each please pls lah la only
""".split())
_PERIOD_WORDS = frozenset("the this that last next first second third fourth each every per".split())
_TIME_UNITS = frozenset("hour hours hourly time day days week weeks month months year years past".split())

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_NAME_MONTHS = frozenset({"april", "may", "june"})  # also first names: "send May 20" is a payment
_DATE_PREP = r"(?:on|by|before|after|until|till|from|since|in|of|due|dated)"
_SP = r"[^\S\n]"  # whitespace within a line

_JOIN = re.compile(rf"{_SP}+|{_SP}*[-–]{_SP}*")
_SPACES = re.compile(rf"{_SP}+")
_PLAIN_NUMBER = re.compile(r"(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+|[1-9][0-9]*|0)(?:\.[0-9]+)?")

_PERCENT_AFTER = re.compile(rf"{_SP}*(?:%|per{_SP}?cent\b|pct\b)", re.I)
_FRACTION_AFTER = re.compile(rf"{_SP}*[-–]?{_SP}*(?:{'|'.join(_DENOMINATORS)})\b", re.I)
# "500+", "500-ish", "a hundred plus" (but "500 plus the SP bill" is two things)
_VAGUE_AFTER = re.compile(rf"\+(?!{_SP}*[0-9])|[-–]?ish\b|{_SP}+plus\b(?!{_SP}*[A-Za-z0-9$])", re.I)
_VAGUE_BEFORE = re.compile(rf"\b(?:few|couple(?:{_SP}+of)?|several|many|some|dozens?(?:{_SP}+of)?){_SP}+$", re.I)
_TIME_AFTER = re.compile(rf"{_SP}*(?:[ap]\.?{_SP}?m\b\.?|o['’]?clock\b)", re.I)
_TIME_BEFORE = re.compile(rf"\b(?:past|quarter{_SP}+to){_SP}+$", re.I)
_ORDINAL_AFTER = re.compile(rf"{_SP}*[-–]?{_SP}*(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b", re.I)
_MONTH_BEFORE = re.compile(rf"(?P<prep>\b{_DATE_PREP}{_SP}+)?\b(?P<month>{_MONTHS})\.?,?{_SP}+$", re.I)
_MONTH_AFTER = re.compile(rf"(?:st|nd|rd|th)?{_SP}*(?:of{_SP}+)?(?P<month>{_MONTHS})\b", re.I)
_DATE_PREP_BEFORE = re.compile(rf"\b{_DATE_PREP}{_SP}+(?:the{_SP}+)?$", re.I)
_YEAR_BEFORE = re.compile(
    rf"\b(?:in|since|year|until|till|before|after|during|fy|circa|end{_SP}+of|early|mid|late|class{_SP}+of){_SP}+$",
    re.I,
)
# A label right before a number makes it an identifier: "ref 12345", "option 2", "pin 1234".
_ID_LABEL = re.compile(
    r"(?:\b(?:ref(?:erence)?|invoice|policy|option|step|item|choice|page|version|unit|blk|block|flight"
    r"|bus|route|postal|zip|otp|pin|nric|uen|mobile|phone|tel|hp|handphone|whatsapp|number)"
    rf"|\b(?:card|order|booking|tracking|transaction|txn|bill|member(?:ship)?|customer|serial|ic"
    rf"|passport|cheque|check){_SP}*(?:no\.?|number|num|#)"
    r"|#)"
    rf"(?:{_SP}*(?:no\.?|number|num|#|:|is|code))*{_SP}*[:#]?{_SP}*$",
    re.I,
)
# "savings account 500" is Singlish for a transfer; only a long number after "account" is an account number.
_ACCOUNT_LABEL = re.compile(
    rf"\b(?:account|acct|acc|a/c)(?:{_SP}*(?:no\.?|number|num|#|:|is))*{_SP}*[:#]?{_SP}*$", re.I
)
_ENDING = re.compile(
    rf"\b(?:ending|ends|last{_SP}+(?:4|four|3|three)(?:{_SP}+digits?)?)"
    rf"(?:{_SP}+(?:in|with|is|are|of))?{_SP}*[:#]?{_SP}*$",
    re.I,
)
_DIGIT_FRACTION = re.compile(r"([1-9])/([1-9][0-9]?)")


# --- Tokens --------------------------------------------------------------------


class _Tok(NamedTuple):
    kind: str  # "num", "word" or "sym"
    text: str
    start: int
    end: int

    @property
    def low(self) -> str:
        return self.text.lower()


# Digit runs keep their inner separators, so "93-1182-44", "3:30" and "25/9"
# arrive as one token and can be rejected whole.
_TOKEN = re.compile(
    r"(?P<num>[0-9]+(?:[.,:/-][0-9]+)*)"
    r"|(?P<word>[A-Za-z]+(?:['’][A-Za-z]+)*)"
    r"|(?P<sym>[$%+#~€£¥₹฿₱₫])"
)


def _tokenize(text: str) -> list[_Tok]:
    return [_Tok(m.lastgroup or "", m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text)]


class _Text:
    """Tokens plus the raw text between them."""

    def __init__(self, text: str):
        self.text = text
        self.toks = _tokenize(text)
        self.n = len(self.toks)

    def word(self, i: int) -> str | None:
        return self.toks[i].low if 0 <= i < self.n and self.toks[i].kind == "word" else None

    def joined(self, i: int, *, hyphen: bool = True) -> bool:
        """Token i continues the phrase before it: only spaces (or one hyphen) between."""
        if not 0 < i < self.n:
            return False
        gap = self.text[self.toks[i - 1].end : self.toks[i].start]
        return (_JOIN if hyphen else _SPACES).fullmatch(gap) is not None

    def attached(self, i: int) -> bool:
        return 0 < i < self.n and self.toks[i - 1].end == self.toks[i].start

    def word_run(self, i: int, limit: int = 16) -> list[int]:
        """Indices of the word tokens joined into one phrase from i."""
        run = [i]
        while len(run) < limit and self.word(run[-1] + 1) is not None and self.joined(run[-1] + 1):
            run.append(run[-1] + 1)
        return run

    def number_run_end(self, i: int) -> int:
        """Index after the numbers joined to token i: "8123 4567", "four five two one".

        Digits run with digits and words with words, so "send mum 500 two days
        later" keeps its 500.
        """
        digits = self.toks[i].kind == "num"
        j = i
        while j + 1 < self.n and self.joined(j + 1) and (
            self.toks[j + 1].kind == "num" if digits else self.word(j + 1) in _RUN_WORDS
        ):
            j += 1
        return j + 1

    def before(self, i: int) -> str:
        return self.text[: self.toks[i].start]

    def after(self, end: int) -> str:
        return self.text[self.toks[end - 1].end :]


# --- Word numbers --------------------------------------------------------------


class _Cardinal(NamedTuple):
    value: int
    used: int  # words consumed
    scaled: bool  # said with "hundred" or a bigger scale word
    money_scale: bool  # said with "grand", "k" or "mil"


def _cardinal(words: Sequence[str], *, scales: bool = True) -> _Cardinal | None:
    """The longest English cardinal at the start of `words`: "two hundred and fifty".

    A small state machine over word classes, so "five fifty" stops after "five"
    (the caller decides what the pair means) instead of silently adding up to 55.
    """
    total = chunk = 0
    last: str | None = None  # class of the previous word
    ceiling: int | None = None  # big scales must shrink: "a million two thousand"
    used = i = 0
    scaled = money = False
    while i < len(words):
        w = words[i]
        after_group = last in (None, "hundred", "big", "and")
        if w in _UNITS and (after_group or last == "ten"):
            chunk += _UNITS[w]
            last = "unit"
        elif w in _TEENS and after_group:
            chunk += _TEENS[w]
            last = "teen"
        elif w in _TENS and after_group:
            chunk += _TENS[w]
            last = "ten"
        elif w == "hundred" and scales and last in (None, "a", "unit", "teen", "ten") and chunk < 100:
            chunk = (chunk or 1) * 100  # "fifteen hundred", "a hundred", Singlish "hundred"
            last, scaled = "hundred", True
        elif (
            w in _BIG
            and scales
            and (ceiling is None or _BIG[w] < ceiling)
            and (last in ("a", "unit", "teen", "ten", "hundred") or (last is None and w == "thousand"))
        ):
            total += (chunk or 1) * _BIG[w]
            chunk, ceiling = 0, _BIG[w]
            last, scaled = "big", True
            money = money or w in _MONEY_SCALES
        elif w == "a" and i == 0 and scales and len(words) > 1 and (words[1] == "hundred" or words[1] in _BIG):
            last = "a"
            i += 1
            continue
        elif w == "and" and last in ("hundred", "big") and i + 1 < len(words) and words[i + 1] in _SMALL:
            last = "and"
            i += 1
            continue
        else:
            break
        i += 1
        used = i
    if used == 0:
        return None
    return _Cardinal(total + chunk, used, scaled, money)


# --- Amounts -------------------------------------------------------------------


@dataclass(frozen=True)
class _Core:
    """The number at one position, before currency words and context."""

    end: int  # token index after it
    values: tuple[Decimal, ...] = ()  # empty: a number, but not money; consumed whole
    digits: str | None = None  # the plain digit string ("2026"), for year and phone rules
    money_scale: bool = False
    scale_first: bool = False  # "hundred" with no number before it: "a few hundred"
    lone_one: bool = False  # "one" alone is nearly always a pronoun ("the joint one")
    paired_first: int | None = None  # "two fifty": the first group, 2


def _read_amount(t: _Text, i: int) -> tuple[int, tuple[Decimal, ...]] | None:
    """(token index after the amount, its S$ readings) for the number starting at token i.

    No readings means a number that is not S$ money (phone, year, "12 shares",
    US$): its tokens are consumed so no part of it is read as money later.
    None means no number starts here.
    """
    currency, at = _currency_prefix(t, i)
    if at >= t.n or (currency and not (t.attached(at) or t.joined(at, hyphen=False))):
        return None
    core = _read_digits(t, at) or _read_words(t, at)
    if core is None:
        return None
    end, values = core.end, core.values
    if not values:
        return end, ()

    suffix = _currency_suffix(t, end)
    if suffix is not None:
        kind, end = suffix
        if kind == "cents":
            values = tuple(v / 100 for v in values)
        elif kind == "sgd" and t.word(end - 1) in _DOLLAR_WORDS:
            cents = _cents_after(t, end)
            if cents is not None and all(v == v.to_integral_value() for v in values):
                values = tuple(v + Decimal(cents[0]) / 100 for v in values)
                end = cents[1]
        if core.paired_first is not None:
            values = values[:1]  # "two fifty dollars" is S$250; nobody says it for S$2.50
        currency = "foreign" if "foreign" in (currency, kind) else (currency or kind)

    if currency == "foreign" or _why_not_money(t, i, end, core, has_currency=currency is not None):
        return end, ()
    exact = (_exact_money(v) for v in values)
    return end, tuple(dict.fromkeys(v for v in exact if v is not None))


def _read_digits(t: _Text, i: int) -> _Core | None:
    tok = t.toks[i]
    if tok.kind != "num":
        return None
    run_end = t.number_run_end(i)
    if run_end > i + 1:
        return _Core(run_end)  # "8123 4567", "65 8123 4567", "50 - 60"
    if _identifier_char_before(t, i):
        return _Core(i + 1)  # "+65", "A123", "t1", "D05", "****4521"
    value = _parse_plain_number(tok.text)
    if value is None:
        return _Core(i + 1)  # "93-1182-44", "3:30", "25/9", "1.2.3", "007"
    nxt = i + 1
    w = t.word(nxt)
    if w is not None and t.attached(nxt):
        if w in _DIGIT_SCALES:
            return _Core(nxt + 1, (value * _DIGIT_SCALES[w],), money_scale=w in _MONEY_SCALES)
        if w not in _DOLLAR_WORDS and w != "sgd":
            return _Core(nxt + 1)  # "3pm", "25th", "5km", "100s"
    elif w is not None and t.joined(nxt, hyphen=False) and w in _DIGIT_SCALES and w != "m":
        if not (w == "grand" and t.word(nxt + 1) == "total"):
            return _Core(nxt + 1, (value * _DIGIT_SCALES[w],), money_scale=w in _MONEY_SCALES)
    return _Core(i + 1, (value,), digits=tok.text if tok.text.isdigit() else None)


def _read_words(t: _Text, i: int) -> _Core | None:
    if t.word(i) is None:
        return None
    run = t.word_run(i)
    words = [t.toks[k].low for k in run]
    if "grand" in words:  # "a grand total of 50" is not S$1,000
        cut = next((k for k, w in enumerate(words[:-1]) if w == "grand" and words[k + 1] == "total"), None)
        words = words[:cut] if cut is not None else words

    def end_at(used: int) -> int:
        return run[used - 1] + 1

    if len(words) >= 2 and words[0] == words[1] and words[0] in ("fifty", "half"):
        return _Core(end_at(2))  # "fifty-fifty" is a split, see extract_fractions
    if len(words) >= 3 and words[0] == "half" and words[1] in ("a", "an") and words[2] in _WORD_SCALES:
        half = Decimal(_WORD_SCALES[words[2]]) / 2  # "half a grand", "half a million"
        return _Core(end_at(3), (half,), money_scale=words[2] in _MONEY_SCALES)
    if len(words) >= 2 and words[0] == "a" and words[1] in _DOLLAR_WORDS:
        return _Core(end_at(1), (Decimal(1),))  # "a dollar fifty"; the suffix reader takes "dollar"

    card = _cardinal(words)
    if card is None:
        return None
    k, value = card.used, Decimal(card.value)
    rest = words[k:]

    if rest[:1] == ["point"]:  # "one point five k"
        decimals = list(takewhile(_DIGIT_WORDS.__contains__, rest[1:]))
        if decimals:
            value = Decimal(f"{card.value}." + "".join(str(_DIGIT_WORDS[d]) for d in decimals))
            k += 1 + len(decimals)
            scale = words[k] if k < len(words) and words[k] in _BIG else None
            if scale:
                return _Core(end_at(k + 1), (value * _BIG[scale],), money_scale=scale in _MONEY_SCALES)
            return _Core(end_at(k), (value,))

    half = 3 if rest[:3] == ["and", "a", "half"] else 2 if rest[:2] == ["and", "half"] else 0
    if half and not card.scaled:
        after = rest[half] if len(rest) > half else None
        if after in _WORD_SCALES:  # "two and a half k", Singlish "one and half k"
            scale = _WORD_SCALES[after]
            return _Core(end_at(k + half + 1), ((value + Decimal("0.5")) * scale,), money_scale=after in _MONEY_SCALES)
        if after in _DOLLAR_WORDS:  # "two and a half dollars"
            return _Core(end_at(k + half), (value + Decimal("0.5"),))
        return _Core(end_at(k + half))  # "two and a half years": a mixed number, not money

    if rest and rest[0] in _RUN_WORDS:
        second = _cardinal(rest, scales=False)
        if not card.scaled and card.value <= 99 and second and 10 <= second.value <= 99:
            k2 = k + second.used
            scale = words[k2] if k2 < len(words) and words[k2] in _BIG else None
            if scale:  # "two fifty k" = 250,000
                value = Decimal((card.value * 100 + second.value) * _BIG[scale])
                return _Core(end_at(k2 + 1), (value,), money_scale=scale in _MONEY_SCALES)
            hundreds = Decimal(card.value * 100 + second.value)
            price = Decimal(card.value) + Decimal(second.value) / 100
            return _Core(end_at(k2), (hundreds, price), paired_first=card.value)
        return _Core(t.number_run_end(end_at(k) - 1))  # "four five two one": digits read out

    return _Core(
        end_at(k),
        (value,),
        money_scale=card.money_scale,
        scale_first=words[0] in ("hundred", "thousand"),
        lone_one=words[:k] == ["one"],
    )


def _currency_prefix(t: _Text, i: int) -> tuple[str | None, int]:
    """("sgd" | "foreign" | None, token index where the number should start)."""
    tok = t.toks[i]
    if tok.kind == "sym":
        if tok.text == "$":
            return "sgd", i + 1  # a bare "$" in Singapore is S$
        if tok.text in _FOREIGN_SYMBOLS:
            return "foreign", i + 1
    elif tok.kind == "word":
        w = tok.low
        if i + 1 < t.n and t.toks[i + 1].text == "$" and t.attached(i + 1):
            if w in ("s", "sg", "sgd"):
                return "sgd", i + 2
            if w in _FOREIGN_DOLLAR_PREFIXES and (len(w) > 1 or tok.text.isupper()):
                return "foreign", i + 2  # "US$", "HK$", "A$"
        elif w == "sgd":
            return "sgd", i + 1
        elif w in _FOREIGN_CODES:
            return "foreign", i + 1
    return None, i


def _currency_suffix(t: _Text, i: int) -> tuple[str, int] | None:
    """("sgd" | "cents" | "foreign", token index after the currency words) at token i."""
    w = t.word(i)
    if w is None or not (t.joined(i, hyphen=False) or t.attached(i)):
        return None
    nxt = t.word(i + 1) if t.joined(i + 1, hyphen=False) else None
    if w in _DOLLAR_WORDS or w == "sgd":
        return "sgd", i + 1
    if w in ("cent", "cents"):
        return "cents", i + 1
    if w in _FOREIGN_WORDS:
        return "foreign", i + 1
    if nxt in _DOLLAR_WORDS:
        if w in _SGD_QUALIFIERS:
            return "sgd", i + 2
        if w in _FOREIGN_QUALIFIERS:
            return "foreign", i + 2
    if w == "hong" and nxt == "kong":
        return "foreign", i + 2
    return None


def _cents_after(t: _Text, i: int) -> tuple[int, int] | None:
    """"... dollars [and] twenty [cents]" at token i: (cents, token index after them)."""
    j = i
    has_and = t.word(j) == "and" and t.joined(j, hyphen=False)
    if has_and:
        j += 1
    if j >= t.n or not t.joined(j, hyphen=False):
        return None
    if t.toks[j].kind == "num":
        if not re.fullmatch(r"[0-9]{1,2}", t.toks[j].text) or (t.word(j + 1) and t.attached(j + 1)):
            return None
        cents, j = int(t.toks[j].text), j + 1
    else:
        run = t.word_run(j)
        card = _cardinal([t.toks[k].low for k in run], scales=False)
        if card is None:
            return None
        cents, j = card.value, run[card.used - 1] + 1
    if not 1 <= cents <= 99:
        return None
    if t.word(j) in ("cent", "cents") and t.joined(j, hyphen=False):
        return cents, j + 1
    if has_and or cents < 10:
        return None  # "fifty dollars and twenty" is two amounts; "one dollar five" is unclear
    # "twelve dollars fifty": unless what follows makes "fifty" an amount of its own.
    if j < t.n and t.joined(j):
        w = t.word(j)
        if t.toks[j].kind == "num" or w in _RUN_WORDS or w in _DIGIT_SCALES or w in _DOLLAR_WORDS \
                or w in _COUNT_NOUNS or w in ("to", "into", "sgd", "cents"):
            return None
    return cents, j


# --- Context: is this number money? --------------------------------------------


def _why_not_money(t: _Text, i: int, end: int, core: _Core, *, has_currency: bool) -> str | None:
    """Why the number at tokens i..end is not an S$ amount, or None if it is."""
    before, after = t.before(i), t.after(end)
    value = core.values[0]
    whole = value == value.to_integral_value()
    if _PERCENT_AFTER.match(after):
        return "percent"
    if whole and value <= 10 and _FRACTION_AFTER.match(after):
        return "fraction numerator"  # "two thirds", "one quarter"
    if _VAGUE_AFTER.match(after) or (core.scale_first and _VAGUE_BEFORE.search(before)):
        return "vague"
    if not has_currency:
        if _TIME_AFTER.match(after) or _TIME_BEFORE.search(before):
            return "time"
        if _counted(t, end):
            return "count"
    if has_currency or core.money_scale:
        return None

    if _ID_LABEL.search(before) or _ENDING.search(before):
        return "identifier"
    if core.digits and (len(core.digits) >= 8 or (len(core.digits) >= 5 and _ACCOUNT_LABEL.search(before))):
        return "identifier"  # phone, card and account numbers
    if whole and _is_date(before, after, int(value)):
        return "date"
    if whole and 1900 <= value <= 2099:
        if _YEAR_BEFORE.search(before):
            return "year"
        if core.digits and len(core.digits) == 4 and value % 50:
            return "year"  # a bare "2026"; "send mum 2000" stays money
    if core.paired_first in (19, 20):
        return "year"  # "twenty twenty-six", "nineteen ninety"
    if value in (20, 30) and core.digits is None and _ORDINAL_AFTER.match(after):
        return "date"  # "the twenty fifth"
    if core.lone_one:
        return "pronoun"
    return None


def _is_date(before: str, after: str, value: int) -> bool:
    day, year = 1 <= value <= 31, 1900 <= value <= 2099
    m = _MONTH_BEFORE.search(before)
    if m and (day or year) and (m["month"].lower() not in _NAME_MONTHS or m["prep"]):
        return True
    m = _MONTH_AFTER.match(after)
    return bool(
        m and day and (m["month"].lower() not in _NAME_MONTHS or _DATE_PREP_BEFORE.search(before))
    )


def _counted(t: _Text, end: int) -> bool:
    """A count noun follows within a few words: "12 shares", "12 more DBS shares"."""
    for j in range(end, min(end + 4, t.n)):
        w = t.word(j)
        if not t.joined(j, hyphen=False) or w is None:
            return False
        if w in _COUNT_NOUNS:
            return True
        if w in _BREAKERS or w in _RUN_WORDS or w in _DOLLAR_WORDS or w in _DIGIT_SCALES:
            return False
    return False


def _identifier_char_before(t: _Text, i: int) -> bool:
    """Glued to letters or a symbol that makes it an identifier: "A123", "+65", "****4521"."""
    if t.attached(i):
        prev = t.toks[i - 1]
        if prev.kind == "word" and prev.low != "sgd":
            return True
        if prev.kind == "sym" and prev.text in "+#":
            return True
    start = t.toks[i].start
    if start == 0:
        return False
    ch = t.text[start - 1]
    if ch in "•*●·_@":
        return True
    return ch in "-/." and start >= 2 and t.text[start - 2].isalnum()


def _parse_plain_number(text: str) -> Decimal | None:
    """ "1,200.50" -> 1200.50; None for anything that is not one plain number."""
    if not _PLAIN_NUMBER.fullmatch(text):
        return None
    return Decimal(text.replace(",", ""))


def _exact_money(value: Decimal) -> Decimal | None:
    """`value` at 2 d.p. if it is a positive whole number of cents; never rounds."""
    try:
        if not value.is_finite() or value <= 0:
            return None
        cents = value.quantize(CENT)
    except InvalidOperation:
        return None
    return cents if cents == value else None


# --- Fractions -----------------------------------------------------------------


def _read_fraction(t: _Text, i: int) -> tuple[int, int, int, int] | None:
    """(numerator, denominator, start, end) for a fraction starting at token i."""
    tok = t.toks[i]
    w = t.word(i)

    # Splits: "fifty-fifty", "half-half" (Singlish), "50/50".
    if w in ("fifty", "half") and t.word(i + 1) == w and t.joined(i + 1):
        return 1, 2, tok.start, t.toks[i + 1].end
    if tok.kind == "num" and tok.text in ("50/50", "50-50"):
        return 1, 2, tok.start, tok.end

    percent = _read_percent(t, i)
    if percent is not None:
        return percent

    # "a quarter", "one third", "two thirds", "3 quarters"
    numerator = None
    if w in ("a", "an"):
        numerator = 1
    elif w in _UNITS or w == "ten":
        numerator = _SMALL[w]
    elif tok.kind == "num" and re.fullmatch(r"[1-9]", tok.text) and not _identifier_char_before(t, i):
        numerator = int(tok.text)
    denominator = t.word(i + 1)
    if numerator is not None and denominator in _DENOMINATORS and t.joined(i + 1):
        if _mixed_number(t, i):
            return None  # "two and a half"
        if denominator == "quarter" and t.word(i + 2) in ("past", "to"):
            return None  # "a quarter past three"
        if numerator == 1 and not _fraction_follows(t, i + 2):
            return None  # "a third party", "a half hour"
        return numerator, _DENOMINATORS[denominator], tok.start, t.toks[i + 1].end

    prev = t.word(i - 1) if t.joined(i) else None
    if w == "half":
        if prev in ("a", "an", "one") or _mixed_number(t, i):
            return None  # read (or rejected) with its article; "one and half"
        nxt = t.word(i + 1) if t.joined(i + 1) else None
        nxt2 = t.word(i + 2) if t.joined(i + 2) else None
        if nxt in _TIME_UNITS or (nxt in ("a", "an") and nxt2 in _TIME_UNITS):
            return None  # "half past", "half an hour"
        return 1, 2, tok.start, tok.end
    if w in ("quarter", "third") and t.word(i + 1) == "of" and t.joined(i + 1):
        if prev in _PERIOD_WORDS or prev in ("a", "an", "one") or prev in _SMALL:
            return None  # "the third of October", "last quarter of the year"
        return 1, _DENOMINATORS[w], tok.start, tok.end

    m = _DIGIT_FRACTION.fullmatch(tok.text) if tok.kind == "num" else None
    if m and int(m[1]) < int(m[2]) <= 12 and not _DATE_PREP_BEFORE.search(t.before(i)):
        return int(m[1]), int(m[2]), tok.start, tok.end  # "1/2 of it", but not "on 1/2"
    return None


def _read_percent(t: _Text, i: int) -> tuple[int, int, int, int] | None:
    tok = t.toks[i]
    if tok.kind == "num":
        value = None if _identifier_char_before(t, i) else _parse_plain_number(tok.text)
        last = i
    elif tok.kind == "word":
        run = t.word_run(i)
        card = _cardinal([t.toks[k].low for k in run])
        value = Decimal(card.value) if card else None
        last = run[card.used - 1] if card else i
    else:
        return None
    if value is None or value <= 0:
        return None
    m = _PERCENT_AFTER.match(t.text, t.toks[last].end)
    if m is None:
        return None
    exponent = value.normalize().as_tuple().exponent
    scale = 10 ** -exponent if isinstance(exponent, int) and exponent < 0 else 1
    return int(value * scale), 100 * scale, tok.start, m.end()  # "12.5%" -> 125/1000


def _fraction_follows(t: _Text, j: int) -> bool:
    return j >= t.n or not t.joined(j) or t.word(j) in _FRACTION_FOLLOWERS


def _mixed_number(t: _Text, i: int) -> bool:
    """Token i follows "<number> and": "two and a half", but not "joint and half to shares"."""
    return (
        i >= 2
        and t.word(i - 1) == "and"
        and t.joined(i)
        and t.joined(i - 1)
        and (t.toks[i - 2].kind == "num" or t.word(i - 2) in _SMALL)
    )


# --- Confusables ---------------------------------------------------------------

_TEEN_TY = {
    "thirteen": "thirty", "fourteen": "forty", "fifteen": "fifty", "sixteen": "sixty",
    "seventeen": "seventy", "eighteen": "eighty", "nineteen": "ninety",
}
_TEEN_TY |= {ty: teen for teen, ty in _TEEN_TY.items()}
_SAY_UNITS = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_SAY_TEENS = {v: w for w, v in _TEENS.items()}
_SAY_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _say(n: int) -> list[str]:
    """Canonical spoken words for 0 <= n < 10**12 ("one thousand five hundred")."""
    if not 0 <= n < 10**12:
        return []
    words: list[str] = []
    for scale, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand"), (1, "")):
        group, n = divmod(n, scale)
        if group:
            hundreds, rest = divmod(group, 100)
            if hundreds:
                words += [_SAY_UNITS[hundreds], "hundred"]
            if 10 <= rest <= 19:
                words.append(_SAY_TEENS[rest])
            elif rest:
                words += [w for w in (_SAY_TENS[rest // 10], _SAY_UNITS[rest % 10]) if w]
            if name:
                words.append(name)
    return words


def _teen_ty_swaps(n: int) -> set[int]:
    """Numbers whose spoken form differs from n's by one teen/ty word ("fifteen" <-> "fifty")."""
    words = _say(n)
    found = set()
    for k, w in enumerate(words):
        if w in _TEEN_TY:
            swapped = [*words[:k], _TEEN_TY[w], *words[k + 1 :]]
            card = _cardinal(swapped)
            # "fifty five" -> "fifteen five" is not a number, so 55 has no teen/ty twin.
            if card is not None and card.used == len(swapped):
                found.add(card.value)
    return found
