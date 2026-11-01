from pathlib import Path

from services import doc_parser, hermes_tools


def _tool_output_root(tmp_path, monkeypatch) -> Path:
    fake_module = tmp_path / "services" / "hermes_tools.py"
    fake_module.parent.mkdir(parents=True)
    monkeypatch.setattr(hermes_tools, "__file__", str(fake_module))
    output = tmp_path / "output"
    output.mkdir()
    return output


def test_document_tool_rejects_same_prefix_sibling(tmp_path, monkeypatch):
    _tool_output_root(tmp_path, monkeypatch)
    sibling = tmp_path / "output_evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")

    result = hermes_tools._parse_document_tool({
        "file_path": "../output_evil/secret.txt",
    })

    assert result == "非法文件路径。"


def test_document_tool_rejects_file_before_unbounded_read(tmp_path, monkeypatch):
    output = _tool_output_root(tmp_path, monkeypatch)
    candidate = output / "large.docx"
    candidate.write_bytes(b"12345")
    monkeypatch.setattr(hermes_tools, "_DOCUMENT_TOOL_MAX_BYTES", 4)

    result = hermes_tools._parse_document_tool({"file_path": candidate.name})

    assert "超过 20MB 限制" in result


def test_document_tool_disables_eager_image_base64(tmp_path, monkeypatch):
    output = _tool_output_root(tmp_path, monkeypatch)
    candidate = output / "image.png"
    candidate.write_bytes(b"image")
    observed = {}

    def parse(raw, filename, *, encode_images):
        observed.update({
            "raw": raw,
            "filename": filename,
            "encode_images": encode_images,
        })
        return {"kind": "image", "mime": "image/png", "image_bytes": raw}

    monkeypatch.setattr(doc_parser, "parse_document", parse)

    result = hermes_tools._parse_document_tool({"file_path": candidate.name})

    assert "需走识图通道" in result
    assert observed == {
        "raw": b"image",
        "filename": "image.png",
        "encode_images": False,
    }
