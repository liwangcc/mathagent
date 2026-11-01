from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _assert_succeeded(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )


def test_server_validation_imports_do_not_load_image_runtimes():
    completed = _run_isolated(
        """
        import sys

        import services.accelerator_artifacts
        import services.template_evidence
        import services.accelerator_validation

        loaded_roots = {name.partition(".")[0] for name in sys.modules}
        assert not ({"cv2", "numpy", "PIL"} & loaded_roots), loaded_roots
        """
    )

    _assert_succeeded(completed)


def test_server_template_contract_paths_do_not_load_image_runtimes():
    completed = _run_isolated(
        """
        import hashlib
        from pathlib import Path
        import struct
        import sys
        import tempfile
        import zlib

        from services.accelerator_artifacts import validate_uploaded_artifact_file
        from services.template_evidence import (
            accept_canonical_template_candidate,
            build_template_artifact,
            verify_template_artifacts,
        )
        import services.accelerator_validation


        def png_chunk(kind, payload):
            checksum = zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


        def write_rgb_png(path, width, height):
            compressor = zlib.compressobj(level=6)
            encoded = bytearray()
            row = b"\\x00" + b"\\xff\\xff\\xff" * width
            for _ in range(height):
                encoded.extend(compressor.compress(row))
            encoded.extend(compressor.flush())
            header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
            path.write_bytes(
                b"\\x89PNG\\r\\n\\x1a\\n"
                + png_chunk(b"IHDR", header)
                + png_chunk(b"IDAT", bytes(encoded))
                + png_chunk(b"IEND", b"")
            )


        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source.jpg"
            source.write_bytes(b"immutable-client-source")
            canonical = workspace / "canonical.png"
            write_rgb_png(canonical, 2064, 2064)
            canonical_manifest = validate_uploaded_artifact_file(
                canonical,
                "canonical_template",
            )
            metadata = {
                "version": "paper-canonical-v1",
                "algorithm": "opencv_page_quad_v1",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_format": "jpeg",
                "exif_orientation": 1,
                "encoded_width": 2000,
                "encoded_height": 2000,
                "source_width": 2000,
                "source_height": 2000,
                "source_coordinate_space": "exif_display_pixels_top_left",
                "canonical_coordinate_space": "canonical_template_v1",
                "source_to_canonical": [
                    [1.0, 0.0, 32.0],
                    [0.0, 1.0, 32.0],
                    [0.0, 0.0, 1.0],
                ],
                "detected_page_quad": [
                    [0.0, 0.0],
                    [1999.0, 0.0],
                    [1999.0, 1999.0],
                    [0.0, 1999.0],
                ],
                "detection_method": "full_frame_scan",
                "confidence": 1.0,
                "page_margin_px": 32,
                "canonical_sha256": canonical_manifest["sha256"],
                "canonical_width": canonical_manifest["width"],
                "canonical_height": canonical_manifest["height"],
            }
            template_dir = workspace / "template"
            accepted = accept_canonical_template_candidate(
                source,
                canonical,
                metadata,
                template_dir,
                0,
            )
            artifact = build_template_artifact(
                accepted.image_path,
                template_dir,
                0,
                [],
                canonicalization=accepted.canonicalization,
            )
            verified = verify_template_artifacts(
                template_dir,
                {"page_1": artifact},
            )
            assert verified["page_1"]["sha256"] == canonical_manifest["sha256"]
            for kind in (
                "canonical_template",
                "template_ocr_evidence",
                "evidence",
            ):
                assert validate_uploaded_artifact_file(canonical, kind)["sha256"] == (
                    canonical_manifest["sha256"]
                )

        loaded_roots = {name.partition(".")[0] for name in sys.modules}
        assert not ({"cv2", "numpy", "PIL"} & loaded_roots), loaded_roots
        """
    )

    _assert_succeeded(completed)


def test_app_startup_does_not_load_optional_image_or_document_runtimes():
    completed = _run_isolated(
        """
        import sys

        import app

        loaded_roots = {name.partition(".")[0] for name in sys.modules}
        forbidden = {
            "PIL",
            "cv2",
            "docx",
            "fitz",
            "matplotlib",
            "numpy",
            "openpyxl",
            "pptx",
            "reportlab",
        }
        assert not (forbidden & loaded_roots), forbidden & loaded_roots
        """
    )

    _assert_succeeded(completed)
