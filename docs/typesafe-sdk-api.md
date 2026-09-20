# typesafe-sdk 0.7.0 — VERIFIED API surface

Introspected from the installed package on 2026-09-21. **The PyPI README is WRONG**:
it shows `response.choices[...]`; the real attribute is `response.answers[...]`,
a discriminated union keyed by your question name. Trust this file, not the README.

```
TypeSafeClient(*, api_key: str | None = None, model: str | None = None, retry: typesafe_sdk._core.retry.RetryPolicy | None = None, timeout: float | httpx2.Timeout | None = None, headers: collections.abc.Mapping[str, str] | None = None, transport: httpx2.BaseTransport | None = None, http_client: httpx2.Client | None = None, base_url: str | None = None) -> None

AsyncTypeSafeClient(*, api_key: str | None = None, model: str | None = None, retry: typesafe_sdk._core.retry.RetryPolicy | None = None, timeout: float | httpx2.Timeout | None = None, headers: collections.abc.Mapping[str, str] | None = None, transport: httpx2.AsyncBaseTransport | None = None, http_client: httpx2.AsyncClient | None = None, base_url: str | None = None) -> None

Choice(*, type: Literal['choice'] = 'choice', instructions: Optional[JSONContent] = None, criteria: collections.abc.Mapping[str, typing.Optional[JSONContent]]) -> None
    .type: typing.Literal['choice']
    .instructions: typing.Optional[JSONContent]
    .criteria: collections.abc.Mapping[str, typing.Optional[JSONContent]]

Score(*, type: Literal['score'] = 'score', instructions: Optional[JSONContent] = None, criteria: collections.abc.Sequence[JSONContent]) -> None
    .type: typing.Literal['score']
    .instructions: typing.Optional[JSONContent]
    .criteria: collections.abc.Sequence[JSONContent]

Noul(*, type: Literal['noul'] = 'noul', instructions: Optional[JSONContent] = None, criteria: typesafe_sdk._core.question_types.NoulCriteria | None = None) -> None
    .type: typing.Literal['noul']
    .instructions: typing.Optional[JSONContent]
    .criteria: typesafe_sdk._core.question_types.NoulCriteria | None

ChoiceAnswer(*, type: Literal['choice'] = 'choice', choice: str, confidence: float, probabilities: dict[str, float]) -> None
    .type: typing.Literal['choice']
    .choice: <class 'str'>
    .confidence: <class 'float'>
    .probabilities: dict[str, float]

ScoreAnswer(*, type: Literal['score'] = 'score', score: float, confidence: float, legend: dict[int, str | dict[str, typing.Any] | list[typing.Any]], probabilities: dict[int, float]) -> None
    .type: typing.Literal['score']
    .score: <class 'float'>
    .confidence: <class 'float'>
    .legend: dict[int, str | dict[str, typing.Any] | list[typing.Any]]
    .probabilities: dict[int, float]

NoulAnswer(*, type: Literal['noul'] = 'noul', noul: float) -> None
    .type: typing.Literal['noul']
    .noul: <class 'float'>

SystemOneResponse(*, model: str, usage: typesafe_sdk._core.response_types.Usage, answers: dict[str, typing.Annotated[typesafe_sdk._core.response_types.NoulAnswer | typesafe_sdk._core.response_types.ChoiceAnswer | typesafe_sdk._core.response_types.ScoreAnswer, FieldInfo(annotation=NoneType, required=True, discriminator='type')]] = <factory>) -> None
    .model: <class 'str'>
    .usage: <class 'typesafe_sdk._core.response_types.Usage'>
    .answers: dict[str, typing.Annotated[typesafe_sdk._core.response_types.NoulAnswer | typesafe_sdk._core.response_types.ChoiceAnswer | typesafe_sdk._core.response_types.ScoreAnswer, FieldInfo(annotation=NoneType, required=True, discriminator='type')]]

Usage(*, input_tokens: int | None = None, output_tokens: int | None = None) -> None
    .input_tokens: int | None
    .output_tokens: int | None

RetryPolicy(max_retries: int = 2, backoff_initial: float = 0.5, backoff_max: float = 5.0, backoff_jitter: float = 0.25, http_statuses: set[int] = <factory>, respect_retry_after: bool = True, api_connection_error: bool = True, api_timeout_error: bool = True, exceptions: set[type[BaseException]] = <factory>, predicate: collections.abc.Callable[[BaseException], bool] | None = None, timeout: float | None = 30.0) -> None

TypeSafeClient.system_one(self, state: JSONContent, questions: collections.abc.Mapping[str, typesafe_sdk._core.question_types.Noul | typesafe_sdk._core.question_types.Choice | typesafe_sdk._core.question_types.Score | typesafe_sdk._core.question_types.NoulModel | typesafe_sdk._core.question_types.ChoiceModel | typesafe_sdk._core.question_types.ScoreModel], *, model: str | None = None, retry: typesafe_sdk._core.retry.RetryPolicy | None = None, timeout: float | httpx2.Timeout | None = None, extra_headers: collections.abc.Mapping[str, str] | None = None, extra_body: collections.abc.Mapping[str, typing.Optional[JSONValue]] | None = None, response_model: type[~ResponseT] | None = None) -> Union[typesafe_sdk._core.response_types.SystemOneResponse, ~ResponseT]
```
