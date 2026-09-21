"""Every word Jev ever sees, gathered in one auditable file.

Wording is a tuning surface, not decoration. Jev's probabilities are calibrated,
so config.py's thresholds are only meaningful against a FIXED question. Rewording
`addressed` silently re-scales `Settings.address_gate`. Keeping every instruction
here means a wording change and a threshold change land in the same diff, and an
eval run can diff the prompts it was scored against.

Question NAMES are constants because they are also the keys of `Answers.answers`
and of the recorded JSONL fixtures ReplayJev reads. A typo in a name is a KeyError
at runtime, not a type error, so nobody spells them by hand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from daa.contracts import Choice, Noul, Question, Score, ToolSpec

# --- names ----------------------------------------------------------------

Q_ADDRESSED = "addressed"
Q_END_OF_TURN = "end_of_turn"
Q_NEEDS_PLANNER = "needs_planner"
Q_STOP = "stop"

Q_TOOL = "tool"

Q_BLAST_RADIUS = "blast_radius"
Q_UNRECOVERABLE = "unrecoverable"
Q_EXPLICITLY_REQUESTED = "explicitly_requested"
Q_TARGET_CONFIDENCE = "target_confidence"

Q_CONSENT = "consent"

# --- shared vocabularies --------------------------------------------------

# The ordered spectrum for blast radius. Index == severity, so a continuous
# score of 2.4 means "between recoverable-with-effort and irreversible" and
# safety/policy.py can threshold on the fraction, not just the bucket.
BLAST_RADIUS_LEVELS: tuple[str, ...] = (
    "read-only",
    "trivially undoable",
    "recoverable with effort",
    "irreversible or externally visible",
)

TARGET_CONFIDENCE_LEVELS: tuple[str, ...] = ("certain", "probable", "guessing")

# The router always offers this escape hatch. Without it a Choice is forced to
# name a tool for "what time is it", and a forced choice over tools is how you
# get `run_shell` activated on small talk.
NONE_OPTION = "none"


# --- the address gate -----------------------------------------------------


def gate_questions() -> dict[str, Question]:
    """The single batched call that runs on EVERY utterance when always-on.

    All three are nouls: each is a yes/no about the same state, and a noul
    returns the bare calibrated probability with no confidence to reconcile.
    Batching them costs almost nothing over asking `addressed` alone.
    """
    return {
        Q_ADDRESSED: Noul(
            instructions=(
                "The assistant is a computer assistant named daa, running on this Mac. "
                "The state is one utterance heard in a room where daa is always listening. "
                "Most of what it hears is not meant for it: people talk to each other, "
                "read aloud, mutter, and talk to their pets. Judge whether THIS utterance "
                "is directed at daa.\n"
                "Two cases decide most of the hard ones:\n"
                "- An utterance aimed at a DIFFERENT assistant (Siri, Alexa, Google, "
                "ChatGPT) is NOT directed at daa, even though it is addressed to an "
                "assistant and sounds exactly like a command.\n"
                "- `context` says what daa was just doing. If daa has just asked a "
                "question or is waiting for a confirmation, then a short reply, a "
                "correction, or an interruption -- 'yeah do it', 'no wait', 'never mind', "
                "'stop' -- IS directed at daa. With no such context, the same words are "
                "probably aimed at a person."
            ),
            # Names the assistant, because the earlier wording ("the assistant")
            # scored "hey siri what's the weather" at 0.98 -- higher than every
            # genuine command in the eval set. Jev was answering correctly; the
            # question simply did not say WHICH assistant. See evals/.
            criteria="the speaker is talking to daa, not to a person and not to another assistant",
        ),
        Q_END_OF_TURN: Noul(
            instructions=(
                "The utterance is a live partial transcript. Judge whether the speaker has "
                "come to the end of what they were saying, rather than pausing mid-thought "
                "to breathe, search for a word, or add a clause."
            ),
            criteria="the speaker has finished their thought",
        ),
        Q_NEEDS_PLANNER: Noul(
            instructions=(
                "Judge whether answering this needs real planning -- several steps, more "
                "than one tool, or a decision about which files or apps are involved -- as "
                "opposed to a single direct reply or one obvious tool call."
            ),
            criteria="fulfilling this request requires multiple steps or tool calls",
        ),
        Q_STOP: Noul(
            instructions=(
                "daa may be part-way through something it was asked to do. Judge whether "
                "this utterance is the speaker telling it to STOP -- 'stop', 'wait', "
                "'no no no', 'cancel that', 'never mind', 'hold on'.\n"
                "Judge the intent to halt, not the politeness of it, and not whether the "
                "speaker gave a reason. A person interrupting a machine that is already "
                "doing something rarely forms a complete sentence."
            ),
            # Read the asymmetry before tuning this. A false stop costs the user
            # a repeat; a missed stop means the thing they are trying to halt
            # carries on. There is no symmetric cost here, so `Settings.stop_p`
            # is deliberately laxer than every other threshold in the product.
            criteria="the speaker wants daa to stop what it is doing right now",
        ),
    }


def gate_state(transcript: str, ctx: Mapping[str, object]) -> dict[str, object]:
    """Unstructured state for the gate. `ctx` is passed through verbatim.

    The caller's context (who spoke last, whether the assistant just asked a
    question, what is on screen) is exactly what disambiguates "yeah, do it"
    from a remark to a colleague, so it is not filtered here.
    """
    return {"utterance": transcript, "context": dict(ctx)}


# --- the tool router ------------------------------------------------------


def tool_choice(specs: Sequence[ToolSpec]) -> Choice:
    """A Choice over tool names, described by each spec's activation_hint.

    `activation_hint` rather than `description` because description is written
    for the conversational LLM ("moves files to the trash") while the hint is
    written for this discrimination ("...not for ejecting disks or quitting
    apps"). Falling back to description keeps an un-hinted tool routable.
    """
    criteria: dict[str, str | None] = {
        spec.name: (spec.activation_hint or spec.description or None) for spec in specs
    }
    criteria[NONE_OPTION] = (
        "no tool applies: the utterance is conversation, a question the assistant can "
        "answer from what it already knows, or chatter"
    )
    return Choice(
        instructions=(
            "Which tool, if any, would the assistant need in order to do what this "
            "utterance asks? Judge by what the user wants to happen, not by which words "
            "they used."
        ),
        criteria=criteria,
    )


def router_state(utterance: str, ctx: Mapping[str, object]) -> dict[str, object]:
    return {"utterance": utterance, "context": dict(ctx)}


# --- the risk gate --------------------------------------------------------


def risk_questions() -> dict[str, Question]:
    """One batched call describing the action from four independent angles.

    They are deliberately redundant: `blast_radius` and `unrecoverable` overlap,
    and that overlap is the point -- safety/policy.py escalates when they
    disagree, which is the cheapest available detector for a bad judgment.
    """
    return {
        Q_BLAST_RADIUS: Score(
            instructions=(
                "How far do the effects of this action reach, and how hard would they be "
                "to take back? Judge the action as RESOLVED against the concrete targets "
                "listed in the state, not the wording of the request."
            ),
            criteria=list(BLAST_RADIUS_LEVELS),
        ),
        Q_UNRECOVERABLE: Noul(
            instructions=(
                "Judge whether the effects of this action would be IMPOSSIBLE, or very "
                "hard, to reverse afterwards.\n"
                "- An action that changes nothing -- reading, listing, searching, "
                "revealing a file, showing a window -- is trivially reversible. No.\n"
                "- This assistant moves files to the Trash rather than deleting them, and "
                "records an inverse for every change it makes, so a move or a trash is "
                "normally recoverable. No, unless something is overwritten with no copy "
                "kept.\n"
                "- Answer yes only for effects no undo can reach: a message or email sent "
                "to another person, a payment, a public post, a permanent deletion.\n"
                "Judge the ACTION's effect, not whether the DATA would be precious."
            ),
            # Polarity: instructions and criteria must point the SAME way. The
            # previous wording asked "could the user get back to where they
            # were" while the criteria asserted "could not be reversed", so a
            # confident "yes, easily recovered" arrived as a high
            # `unrecoverable`. Live Jev scored get_clipboard -- a pure read --
            # at 0.60, and since >0.5 forces CONFIRM_VISUAL that escalated
            # every tool in the product to a screen prompt.
            criteria="the effects of this action could not be undone afterwards",
        ),
        Q_EXPLICITLY_REQUESTED: Noul(
            instructions=(
                "Judge whether the user asked for THIS action on THESE targets, as opposed "
                "to the assistant inferring it from a vaguer request or volunteering it."
            ),
            criteria="the user asked for this exact action on these exact targets",
        ),
        Q_TARGET_CONFIDENCE: Choice(
            instructions=(
                "The state lists the concrete targets the assistant resolved from the "
                "request. How sure should the assistant be that these are the things the "
                "user meant?"
            ),
            criteria={
                "certain": "the user named these targets, or there is exactly one thing "
                "they could have meant",
                "probable": "the resolution is a reasonable reading but another one exists",
                "guessing": "the request was vague, ambiguous, or the targets were picked "
                "by a heuristic such as recency",
            },
        ),
    }


# --- the confirmation parser ---------------------------------------------


def consent_questions() -> dict[str, Question]:
    """Parsing a spoken yes.

    Spoken consent is not string-matchable: "yeah no" is a no, "sure but not
    that one" is a no, "no, the other one" is a correction rather than either.
    Everything the caller needs is the probability, so this is one noul.
    """
    return {
        Q_CONSENT: Noul(
            instructions=(
                "The assistant described an action and asked the user to confirm it. The "
                "reply is a raw voice transcript: casual, disfluent, and sometimes "
                "self-contradicting on the way to its real meaning ('yeah no', 'sure but "
                "not that one', 'wait--'). Judge what the reply actually authorises. A "
                "reply that corrects, narrows, or redirects the action -- 'no, the other "
                "one' -- is NOT consent to the action as described."
            ),
            criteria="the speaker is consenting to the described action",
        ),
    }
