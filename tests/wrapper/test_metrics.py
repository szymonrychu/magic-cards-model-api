"""`ccw_inference_seconds`: wall-clock duration of one model inference (one
`requestId` in the transcript), not one whole turn.

`ccw_turn_duration_seconds` already exists, but it times a whole *match* -
the driver pushes one `claude` turn per match, not per game turn - so it
cannot tell an effort change from noise. This is the metric that can: it is
fed straight from the transcript watcher (`activity.InferenceTimer`), one
observation per inference, labelled by how many output tokens that inference
produced, so a shift toward smaller output-token buckets after an effort
change is visible in production rather than argued from a one-off audit.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from mtg_agent.wrapper.metrics import Metrics


def test_the_inference_histogram_is_registered_under_the_ccw_name() -> None:
    m = Metrics()
    body, _ = m.render()
    text = body.decode()
    assert "ccw_inference_seconds" in text


def test_record_inference_observes_duration_under_its_token_bucket_label() -> None:
    m = Metrics(registry=CollectorRegistry())
    m.record_inference(4.7, 329)
    m.record_inference(16.4, 1360)

    body, _ = m.render()
    text = body.decode()
    # A histogram sample line carries the label and increments a `_bucket`
    # counter for every bucket boundary >= the observed value - the exact
    # boundaries are an implementation choice, but the label value itself
    # (which token range this observation counted against) must appear.
    assert 'output_tokens_bucket="200-500"' in text
    assert 'output_tokens_bucket="1000-2000"' in text
    assert 'ccw_inference_seconds_count{output_tokens_bucket="200-500"} 1.0' in text
    assert 'ccw_inference_seconds_count{output_tokens_bucket="1000-2000"} 1.0' in text


def test_record_inference_buckets_small_and_very_large_outputs_too() -> None:
    m = Metrics(registry=CollectorRegistry())
    m.record_inference(0.9, 40)
    m.record_inference(90.0, 9000)

    body, _ = m.render()
    text = body.decode()
    assert 'output_tokens_bucket="<200"' in text
    assert 'output_tokens_bucket=">=4000"' in text


def test_record_inference_ignores_a_negative_duration() -> None:
    """A clock skew or a mis-ordered transcript line must not poison the
    histogram with a nonsensical negative observation."""
    m = Metrics(registry=CollectorRegistry())
    m.record_inference(-1.0, 100)

    body, _ = m.render()
    text = body.decode()
    assert 'ccw_inference_seconds_count{output_tokens_bucket="<200"} 1.0' not in text


# ---- ccw_model_step_seconds / ccw_tool_seconds / ccw_auto_answers_total ----
#
# Fed from the transcript watcher's `StepTimer` (activity.py), one
# observation per completed tool call: `model_step_seconds` (unlabelled) is
# how long the model took to produce the tool_use, `tool_seconds{tool}` is
# how long the tool itself took to answer.


def test_the_step_histograms_and_the_auto_answer_counter_are_registered() -> None:
    m = Metrics()
    body, _ = m.render()
    text = body.decode()
    assert "ccw_model_step_seconds" in text
    assert "ccw_tool_seconds" in text
    assert "ccw_auto_answers_total" in text


def test_record_step_observes_both_histograms_under_the_tool_label() -> None:
    m = Metrics(registry=CollectorRegistry())
    m.record_step("wait_for_priority", 0.4, 1.5)
    m.record_step("wait_for_priority", 0.1, 0.2)

    body, _ = m.render()
    text = body.decode()
    assert "ccw_model_step_seconds_count 2.0" in text
    assert 'ccw_tool_seconds_count{tool="wait_for_priority"} 2.0' in text


def test_record_step_ignores_a_negative_duration_independently_per_half() -> None:
    """A clock skew that poisons one side of a step must not drop the other
    - each duration is a separate transcript-line measurement. `x`'s
    negative model duration is dropped but its tool duration still counts,
    and vice versa for `y`."""
    m = Metrics(registry=CollectorRegistry())
    m.record_step("x", -1.0, 0.5)
    m.record_step("y", 0.5, -1.0)

    body, _ = m.render()
    text = body.decode()
    assert "ccw_model_step_seconds_count 1.0" in text
    assert 'ccw_tool_seconds_count{tool="x"} 1.0' in text
    assert 'tool="y"' not in text, "a dropped negative tool duration must never label the series"


def test_record_step_counts_auto_answers() -> None:
    m = Metrics(registry=CollectorRegistry())
    m.record_step("decide", 0.1, 0.1, auto_answers=3)
    m.record_step("decide", 0.1, 0.1)

    body, _ = m.render()
    text = body.decode()
    assert "ccw_auto_answers_total 3.0" in text
