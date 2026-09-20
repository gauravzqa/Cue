"""The one file that knows typesafe-sdk exists.

Everything above this file speaks in daa's own frozen dataclasses from
contracts.py. That indirection costs a translation layer and buys two things:
the SDK (six days old) can churn without touching five call sites, and every
test in the suite runs on FakeJev with no key, no socket, and no flake.

Three providers, one Protocol:

    RealJev    live TypeSafe                     synthetic=False
    FakeJev    canned, deterministic, offline    synthetic=True
    ReplayJev  recorded JSONL, for eval runs     synthetic=True
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from daa.config import Settings
from daa.contracts import (
    Answer,
    Answers,
    Choice,
    ChoiceAnswer,
    JevProvider,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)

_TS: Any = None


def _ts() -> Any:
    """Import typesafe-sdk on first real use.

    At module scope this cost 70ms of an 85ms `import daa.jev`, and dragged in
    httpx2 and the whole of pydantic. Only RealJev and the two translation
    functions need it -- FakeJev, ReplayJev and jsonable do not -- so a keyless
    machine running `daa doctor` or `daa say` (the documented fast dev loop)
    was paying for an HTTP client it would never construct. The Protocol seam
    in contracts.py is what makes this safe: nothing outside this file can
    tell the difference.
    """
    global _TS
    if _TS is None:
        import typesafe_sdk

        _TS = typesafe_sdk
    return _TS



__all__ = [
    "FakeJev",
    "JevUnavailable",
    "RealJev",
    "ReplayJev",
    "build_provider",
    "jsonable",
]


class JevUnavailable(RuntimeError):
    """Every way the judgment layer can fail, collapsed into one exception.

    Callers are safety-critical paths (wake, risk, consent) and each of them has
    exactly one correct response to "no judgment available": fail closed. Giving
    them one exception to catch means nobody forgets a subclass and accidentally
    lets a transport error propagate as a crash instead of a refusal.
    """


# ---------------------------------------------------------------------------
# state hygiene
# ---------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """Coerce arbitrary state into something that survives JSON encoding.

    State is unstructured by design, so callers hand us Paths, enums, datetimes
    and dataclasses without thinking. Serialising at the seam means a stray
    Path in a tool's args can never turn into a TypeError deep inside httpx,
    which would surface as a wake failure rather than as the bug it is.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# translation: daa -> SDK
# ---------------------------------------------------------------------------


def _to_sdk_question(q: Question) -> Any:
    if isinstance(q, Choice):
        return _ts().Choice(instructions=q.instructions, criteria=dict(q.criteria))
    if isinstance(q, Score):
        return _ts().Score(instructions=q.instructions, criteria=list(q.criteria))
    if isinstance(q, Noul):
        # The SDK models noul criteria as a {true, false} pair; daa models it as
        # the single sentence that must be true. Populating only `true` keeps
        # daa's Noul honest -- inventing a `false` phrasing here would be this
        # file putting words in the question author's mouth.
        criteria = {"true": q.criteria} if q.criteria else None
        return _ts().Noul(instructions=q.instructions, criteria=criteria)
    raise JevUnavailable(f"unknown question type {type(q).__name__}")


# ---------------------------------------------------------------------------
# translation: SDK -> daa
# ---------------------------------------------------------------------------


def _legend_text(value: Any) -> str:
    # ScoreAnswer.legend values are typed `str | dict | list` by the SDK. daa's
    # ScoreAnswer promises a sequence of strings, so anything richer is flattened
    # rather than leaked upward as an Any.
    return value if isinstance(value, str) else json.dumps(jsonable(value), sort_keys=True)


def _score_from_sdk(raw: Any, question: Score) -> ScoreAnswer:
    """Turn the SDK's int-keyed legend/probability dicts into ordered sequences.

    daa's ScoreAnswer is positional: legend[i] and probabilities[i] describe the
    same level, and the index is the score. The SDK's dicts are unordered and
    can arrive with string keys after a JSON round-trip, so ordering is rebuilt
    from the numeric keys rather than trusted from iteration order.
    """
    try:
        keys = sorted({int(k) for k in raw.legend} | {int(k) for k in raw.probabilities})
    except (TypeError, ValueError) as exc:
        raise JevUnavailable(f"score answer has non-integer levels: {exc}") from exc

    legend_by_key = {int(k): _legend_text(v) for k, v in raw.legend.items()}
    probs_by_key = {int(k): float(v) for k, v in raw.probabilities.items()}

    # A legend the model did not echo back falls back to the level names we
    # asked with, so downstream code can always name the score it acted on.
    fallback = list(question.criteria)
    legend = [
        legend_by_key.get(k) or (fallback[k] if 0 <= k < len(fallback) else str(k)) for k in keys
    ]
    return ScoreAnswer(
        score=float(raw.score),
        legend=tuple(legend),
        probabilities=tuple(probs_by_key.get(k, 0.0) for k in keys),
        confidence=float(raw.confidence),
    )


def _from_sdk_answer(name: str, raw: Any, question: Question) -> Answer:
    if isinstance(question, Choice):
        if not isinstance(raw, _ts().ChoiceAnswer):
            raise JevUnavailable(f"asked {name} as a choice, got {type(raw).__name__}")
        return ChoiceAnswer(
            choice=raw.choice,
            probabilities={str(k): float(v) for k, v in raw.probabilities.items()},
            confidence=float(raw.confidence),
        )
    if isinstance(question, Score):
        if not isinstance(raw, _ts().ScoreAnswer):
            raise JevUnavailable(f"asked {name} as a score, got {type(raw).__name__}")
        return _score_from_sdk(raw, question)
    if isinstance(question, Noul):
        if not isinstance(raw, _ts().NoulAnswer):
            raise JevUnavailable(f"asked {name} as a noul, got {type(raw).__name__}")
        return NoulAnswer(noul=float(raw.noul))
    raise JevUnavailable(f"unknown question type {type(question).__name__}")


# ---------------------------------------------------------------------------
# RealJev
# ---------------------------------------------------------------------------


class RealJev:
    """Live TypeSafe System One. The only thing in daa that opens a socket to it."""

    def __init__(self, client: Any, *, model: str | None = None) -> None:
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> RealJev:
        return cls(_ts().TypeSafeClient(api_key=settings.typesafe_api_key))

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        timeout_s: float = 2.0,
    ) -> Answers:
        if not questions:
            raise JevUnavailable("asked with no questions")

        sdk_questions = {name: _to_sdk_question(q) for name, q in questions.items()}
        started = time.perf_counter()
        try:
            response = self._client.system_one(
                jsonable(state),
                sdk_questions,
                timeout=timeout_s,
                **({"model": self._model} if self._model else {}),
            )
        except _ts().TypeSafeError as exc:
            # Includes auth, rate limit, timeout, connection and response
            # validation errors. They differ in how you'd fix them and not at
            # all in what the caller must do right now.
            raise JevUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defence against SDK churn
            raise JevUnavailable(f"unexpected {type(exc).__name__}: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        raw_answers = getattr(response, "answers", None)
        if not isinstance(raw_answers, Mapping):
            raise JevUnavailable("response carried no answers mapping")

        translated: dict[str, Answer] = {}
        for name, question in questions.items():
            if name not in raw_answers:
                raise JevUnavailable(f"no answer for {name!r}")
            translated[name] = _from_sdk_answer(name, raw_answers[name], question)

        return Answers(answers=translated, latency_ms=latency_ms, synthetic=False)


# ---------------------------------------------------------------------------
# canned answers, shared by FakeJev and ReplayJev
# ---------------------------------------------------------------------------


def _default_answer(question: Question) -> Answer:
    """What an unseeded question answers: maximum uncertainty, every time.

    A test that forgets to seed a question must FAIL, not pass by accident, so
    the defaults sit exactly on the fence: a noul of 0.5 has a derived
    confidence of 0.0, and no threshold in config.py treats it as a yes.
    """
    if isinstance(question, Noul):
        return NoulAnswer(noul=0.5)
    if isinstance(question, Choice):
        keys = list(question.criteria)
        if not keys:
            raise JevUnavailable("choice question has no criteria")
        share = 1.0 / len(keys)
        return ChoiceAnswer(
            choice=keys[0], probabilities={k: share for k in keys}, confidence=0.5
        )
    if isinstance(question, Score):
        levels = list(question.criteria)
        if not levels:
            raise JevUnavailable("score question has no criteria")
        share = 1.0 / len(levels)
        return ScoreAnswer(
            score=(len(levels) - 1) / 2.0,
            legend=tuple(levels),
            probabilities=tuple(share for _ in levels),
            confidence=0.5,
        )
    raise JevUnavailable(f"unknown question type {type(question).__name__}")


def _score_answer_from_value(score: float, question: Score) -> ScoreAnswer:
    """Spread probability over the two levels a continuous score falls between.

    A seed of 2.4 should not look one-hot; downstream code reads the
    distribution as well as the point estimate, and a fixture that lies about
    the shape of the distribution makes the threshold tests meaningless.
    """
    levels = list(question.criteria)
    top = len(levels) - 1
    clamped = min(max(float(score), 0.0), float(top))
    lo = int(clamped)
    hi = min(lo + 1, top)
    frac = clamped - lo
    probs = [0.0] * len(levels)
    probs[lo] += 1.0 - frac
    probs[hi] += frac
    return ScoreAnswer(
        score=clamped,
        legend=tuple(levels),
        probabilities=tuple(probs),
        confidence=max(probs),
    )


def _coerce_answer(name: str, value: Any, question: Question) -> Answer:
    """Accept a fixture written the lazy way as well as a full Answer object.

    Fixtures are written by hand and by the recorder, so both `{"addressed":
    0.94}` and a fully specified ChoiceAnswer have to work. Anything that
    cannot be read as an answer to THIS question raises rather than degrading
    to a default, because a silently mistyped fixture is an untrustworthy eval.
    """
    if isinstance(value, (ChoiceAnswer, ScoreAnswer, NoulAnswer)):
        return value

    if isinstance(value, Mapping):
        data = dict(value)
        data.pop("type", None)
        if isinstance(question, Noul) and "noul" in data:
            return NoulAnswer(noul=float(data["noul"]))
        if isinstance(question, Choice):
            probs = data.get("probabilities")
            if probs is None and all(isinstance(v, (int, float)) for v in data.values()):
                probs = data  # bare {option: p} map
            if probs:
                probs = {str(k): float(v) for k, v in probs.items()}
                choice = str(data.get("choice") or max(probs, key=lambda k: probs[k]))
                conf = float(data.get("confidence", probs.get(choice, 0.0)))
                return ChoiceAnswer(choice=choice, probabilities=probs, confidence=conf)
            if "choice" in data:
                return _coerce_answer(name, data["choice"], question)
        if isinstance(question, Score) and "score" in data:
            spread = _score_answer_from_value(float(data["score"]), question)
            # A recorded record may carry the model's own int-keyed distribution;
            # honour it when present rather than re-deriving a tidier one.
            raw_probs = data.get("probabilities")
            if raw_probs:
                by_level = {int(k): float(v) for k, v in raw_probs.items()}
                probabilities = tuple(
                    by_level.get(i, 0.0) for i in range(len(question.criteria))
                )
            else:
                probabilities = spread.probabilities
            return ScoreAnswer(
                score=spread.score,
                legend=spread.legend,
                probabilities=probabilities,
                confidence=float(data.get("confidence", spread.confidence)),
            )
        raise JevUnavailable(f"canned answer for {name!r} does not fit its question type")

    if isinstance(question, Noul) and isinstance(value, (int, float)) and not isinstance(
        value, bool
    ):
        return NoulAnswer(noul=float(value))
    if isinstance(question, Noul) and isinstance(value, bool):
        return NoulAnswer(noul=1.0 if value else 0.0)
    if isinstance(question, Score) and isinstance(value, (int, float)):
        return _score_answer_from_value(float(value), question)
    if isinstance(question, Choice) and isinstance(value, str):
        keys = list(question.criteria)
        if value not in keys:
            raise JevUnavailable(f"canned choice {value!r} is not an option for {name!r}")
        # A bare option name means "certainly this one"; a fixture that wants a
        # near-tie has to spell out the probabilities, which is the point.
        probs = {k: (1.0 if k == value else 0.0) for k in keys}
        return ChoiceAnswer(choice=value, probabilities=probs, confidence=1.0)

    raise JevUnavailable(f"canned answer for {name!r} does not fit its question type")


# ---------------------------------------------------------------------------
# FakeJev
# ---------------------------------------------------------------------------


class FakeJev:
    """Deterministic offline provider. Every test in the suite runs on this.

    Answers are keyed by question NAME rather than by call index so a test can
    seed `{"addressed": 0.95}` once and stay correct when a later refactor adds
    a fourth question to the same batch.
    """

    def __init__(
        self,
        answers: Mapping[str, Any] | None = None,
        *,
        latency_ms: float = 0.0,
        fail: BaseException | None = None,
    ) -> None:
        self.canned: dict[str, Any] = dict(answers or {})
        # Fixed rather than measured: a synthetic latency that wobbles makes
        # eval output non-reproducible for no benefit.
        self.latency_ms = latency_ms
        self._fail = fail
        # Every (state, questions, timeout) this provider was asked, so a test
        # can assert the always-on gate really is ONE call per utterance.
        self.calls: list[tuple[Mapping[str, Any], Mapping[str, Question], float]] = []

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        timeout_s: float = 2.0,
    ) -> Answers:
        self.calls.append((dict(state), dict(questions), timeout_s))
        if self._fail is not None:
            raise self._fail
        answers = {
            name: (
                _coerce_answer(name, self.canned[name], q)
                if name in self.canned
                else _default_answer(q)
            )
            for name, q in questions.items()
        }
        return Answers(answers=answers, latency_ms=self.latency_ms, synthetic=True)

    # Convenience for tests that drive several turns through one provider.
    def seed(self, **answers: Any) -> FakeJev:
        self.canned.update(answers)
        return self


# ---------------------------------------------------------------------------
# ReplayJev
# ---------------------------------------------------------------------------


class ReplayJev:
    """Replays recorded JSONL, one record per `ask`, in order.

    Eval runs need the SAME judgments every time so a change in policy shows up
    as a change in outcome rather than as model noise. Records are consumed
    sequentially because the thing being replayed is a CONVERSATION: the second
    `addressed` of a session is a different question from the first even though
    it is spelled the same.
    """

    def __init__(self, records: Iterable[Mapping[str, Any]], *, strict: bool = True) -> None:
        self._records: list[Mapping[str, Any]] = [dict(r) for r in records]
        self._index = 0
        # strict=False lets a fixture recorded before a question existed still
        # replay, with the new question answered at maximum uncertainty.
        self._strict = strict

    @classmethod
    def from_path(cls, path: str | Path, *, strict: bool = True) -> ReplayJev:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        return cls(records, strict=strict)

    @property
    def remaining(self) -> int:
        return len(self._records) - self._index

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        timeout_s: float = 2.0,
    ) -> Answers:
        if self._index >= len(self._records):
            raise JevUnavailable("replay fixture exhausted")
        record = self._records[self._index]
        self._index += 1

        canned = record.get("answers", {})
        answers: dict[str, Answer] = {}
        for name, question in questions.items():
            if name in canned:
                answers[name] = _coerce_answer(name, canned[name], question)
            elif self._strict:
                raise JevUnavailable(f"replay record {self._index - 1} has no answer for {name!r}")
            else:
                answers[name] = _default_answer(question)

        return Answers(
            answers=answers,
            latency_ms=float(record.get("latency_ms", 0.0)),
            synthetic=True,
        )


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def build_provider(settings: Settings) -> JevProvider:
    """RealJev when there is a key, FakeJev otherwise.

    The fallback is deliberately silent and deliberately NOT a crash: daa must
    start and be developed on a laptop with no key, and everything downstream
    can already tell the difference because every Answers carries `synthetic`.
    """
    if settings.jev_live:
        return RealJev.from_settings(settings)
    return FakeJev()
