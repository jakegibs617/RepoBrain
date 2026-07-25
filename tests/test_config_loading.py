import json

import pytest

from repobrain.config import RepoBrainConfig


def _write_config(tmp_path, content: str):
    path = tmp_path / ".repobrain" / "config.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    return path


def test_load_rejects_malformed_json_with_project_specific_error(tmp_path):
    path = _write_config(tmp_path, '{"max_file_size_bytes": }')

    with pytest.raises(ValueError) as exc_info:
        RepoBrainConfig.load(tmp_path)

    message = str(exc_info.value)
    assert f"Invalid RepoBrain config at {path}" in message
    assert "malformed JSON" in message
    assert "line 1" in message


@pytest.mark.parametrize("value", ["[]", "null", '"config"'])
def test_load_rejects_non_object_top_level(tmp_path, value):
    path = _write_config(tmp_path, value)

    with pytest.raises(ValueError, match="must contain a JSON object") as exc_info:
        RepoBrainConfig.load(tmp_path)

    assert str(path) in str(exc_info.value)


def test_load_rejects_unknown_keys(tmp_path):
    path = _write_config(
        tmp_path,
        json.dumps({"max_file_size_bytes": 1024, "mystery": True, "typo": 1}),
    )

    with pytest.raises(ValueError) as exc_info:
        RepoBrainConfig.load(tmp_path)

    message = str(exc_info.value)
    assert f"Invalid RepoBrain config at {path}" in message
    assert "unknown keys: mystery, typo" in message


def test_load_accepts_known_keys(tmp_path):
    _write_config(
        tmp_path,
        json.dumps(
            {
                "include_patterns": ["src/**"],
                "max_file_size_bytes": 1024,
                "history_max_commits": 10,
            }
        ),
    )

    config = RepoBrainConfig.load(tmp_path)

    assert config.include_patterns == ["src/**"]
    assert config.max_file_size_bytes == 1024
    assert config.history_max_commits == 10
