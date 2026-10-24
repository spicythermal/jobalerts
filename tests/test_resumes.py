from jobalerts.resumes import MAX_CHARS, load_resumes


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with a line of Helvetica text, built by hand so the test needs no PDF writer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_loads_pdf_markdown_and_text_sorted_by_name(tmp_path):
    (tmp_path / "b_hpc.pdf").write_bytes(tiny_pdf("Particle-In-Cell simulation lead"))
    (tmp_path / "a_rnd.md").write_text("# R&D group leader\n\n\n\nFocused ion beam   systems", encoding="utf-8")
    (tmp_path / "c_short.txt").write_text("Ph.D. physicist", encoding="utf-8")
    (tmp_path / "notes.docx").write_bytes(b"ignored")
    (tmp_path / "~$lockfile.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "empty.txt").write_text("   \n", encoding="utf-8")

    resumes = load_resumes(tmp_path)

    assert [r.name for r in resumes] == ["a_rnd.md", "b_hpc.pdf", "c_short.txt"]
    assert resumes[0].text == "# R&D group leader\n\nFocused ion beam systems"
    assert "Particle-In-Cell simulation lead" in resumes[1].text


def test_broken_pdf_is_skipped_and_long_text_capped(tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    (tmp_path / "long.txt").write_text("x" * (MAX_CHARS + 500), encoding="utf-8")
    resumes = load_resumes(tmp_path)
    assert [r.name for r in resumes] == ["long.txt"]
    assert len(resumes[0].text) == MAX_CHARS


def test_missing_folder_means_no_resumes(tmp_path):
    assert load_resumes(tmp_path / "nope") == []
