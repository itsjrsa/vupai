import json

from vupai import audio

# A trimmed `system_profiler -json SPAudioDataType` payload: two inputs (one the
# default) plus an output-only device that must be excluded.
SAMPLE = json.dumps({
    "SPAudioDataType": [
        {
            "_name": "Devices",
            "_items": [
                {
                    "_name": "MacBook Pro Microphone",
                    "coreaudio_device_input": 1,
                    "coreaudio_default_audio_input_device": "spaudio_yes",
                },
                {
                    "_name": "AirPods Pro",
                    "coreaudio_device_input": 1,
                },
                {
                    "_name": "External Headphones",
                    "coreaudio_device_output": 2,
                },
            ],
        }
    ]
})


def test_list_input_devices_excludes_outputs_and_flags_default():
    devices = audio.list_input_devices(runner=lambda: SAMPLE)
    names = [d.name for d in devices]
    assert names == ["MacBook Pro Microphone", "AirPods Pro"]
    assert devices[0].is_default is True
    assert devices[1].is_default is False


def test_list_input_devices_runner_failure_returns_empty():
    def boom():
        raise OSError("system_profiler missing")

    assert audio.list_input_devices(runner=boom) == []


def test_list_input_devices_bad_json_returns_empty():
    assert audio.list_input_devices(runner=lambda: "not json") == []


def test_resolve_device_empty_means_default_without_enumeration():
    called = False

    def runner():
        nonlocal called
        called = True
        return SAMPLE

    name, warning = audio.resolve_device("", runner=runner)
    assert (name, warning) == ("", None)
    assert called is False  # no enumeration for the default case


def test_resolve_device_present_returns_verbatim():
    name, warning = audio.resolve_device("AirPods Pro", runner=lambda: SAMPLE)
    assert name == "AirPods Pro"
    assert warning is None


def test_resolve_device_absent_falls_back_to_default_with_warning():
    name, warning = audio.resolve_device("Ghost Mic", runner=lambda: SAMPLE)
    assert name == ""
    assert "Ghost Mic" in warning
    assert "system default" in warning
