import io
import json

import pandas as pd
import pytest

import app as appmod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(appmod.app.config, "TESTING", True)
    for info in appmod.API_PROVIDERS.values():
        monkeypatch.delenv(info["env_var"], raising=False)
    monkeypatch.setattr(appmod.requests, "request", lambda *a, **kw: pytest.fail("Unexpected network request"))
    monkeypatch.setattr(appmod.requests, "post", lambda *a, **kw: pytest.fail("Unexpected network request"))
    return appmod.app.test_client()


@pytest.mark.parametrize("path", ["/", "/search", "/settings", "/upload", "/image-transcription", "/batch-transcription", "/model-comparison", "/user-guide", "/codebase_documentation", "/formula-generator"])
def test_local_pages_render_without_account_or_network(client, path):
    response = client.get(path)
    assert response.status_code == 200


def test_hosted_endpoints_are_absent():
    paths = {rule.rule for rule in appmod.app.url_map.iter_rules()}
    assert not any(path.startswith(("/support", "/admin", "/auth")) for path in paths)
    assert not paths.intersection({"/login", "/register", "/logout", "/download/source.zip", "/download/mac", "/download/windows"})


def test_keys_are_local_encrypted_and_shared_by_harvard_providers(client):
    assert client.post("/settings", data={"action": "save_key", "provider": "harvard", "api_key": "local-test-key"}).status_code == 302
    with client.session_transaction() as sess:
        assert sess["api_keys"]["harvard"] != "local-test-key"
        assert appmod.decrypt_api_key(sess["api_keys"]["harvard"]) == "local-test-key"
    client.post("/settings", data={"action": "set_active", "provider": "harvard_gemini"})
    with client:
        client.get("/settings")
        assert appmod.get_active_api_key() == ("local-test-key", "harvard_gemini")
        assert appmod.get_api_key_for_provider("harvard") == "local-test-key"
    client.post("/settings", data={"action": "remove_key", "provider": "harvard_gemini"})
    with client.session_transaction() as sess:
        assert "harvard" not in sess.get("api_keys", {})


def test_stream_uses_explicit_provider_model_and_returns_json(client, tmp_path, monkeypatch):
    image = tmp_path / "scan.jpg"
    image.write_bytes(b"image")
    client.post("/settings", data={"action": "save_key", "provider": "harvard", "api_key": "test-key"})
    captured = {}

    def transcribe(path, key, custom_prompt=None, provider=None, model=None):
        captured.update(provider=provider, model=model)
        return json.dumps({"title": "Test", "city": "Boston", "publisher_name": "Test", "date": ""})

    monkeypatch.setattr(appmod, "image_to_text", transcribe)
    response = client.post("/transcribe-batch-image", data={"image_path": str(image)})
    assert json.loads(response.get_data(as_text=True))["success"]
    assert captured["provider"] == "harvard"
    assert captured["model"]


def test_stream_failure_is_json(client, tmp_path, monkeypatch):
    image = tmp_path / "scan.jpg"
    image.write_bytes(b"image")
    client.post("/settings", data={"action": "save_key", "provider": "harvard", "api_key": "test-key"})

    def fail(*args, **kwargs):
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr(appmod, "image_to_text", fail)
    response = client.post("/transcribe-batch-image", data={"image_path": str(image)})
    assert "Provider unavailable" in json.loads(response.get_data(as_text=True))["error"]


def test_estimate_endpoint_preserves_estimator_result(client, monkeypatch):
    result = {"estimated_year": "1850–1860", "error": None}
    monkeypatch.setattr(appmod, "estimate_year", lambda *args: result)
    response = client.post("/estimate-date", data={"publisher_name": "Test", "plate_number": "123", "city": "Boston"})
    assert response.get_json()["result"] == result


def test_saved_runs_export_csv_and_excel(client, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "BATCH_RESULTS_DIR", str(tmp_path))
    row = {field: "" for field in ["title", "statement_of_responsibility", "edition", "city", "publisher_name", "address", "plate_number", "date", "image_name"]}
    row.update(title="Test score", image_name="scan.jpg", date="1850")
    appmod._save_batch_results("test-run", [row])
    csv = client.get("/export-run/csv?run_id=test-run")
    xlsx = client.get("/export-run/xlsx?run_id=test-run")
    assert csv.status_code == xlsx.status_code == 200
    assert b"Test score" in csv.data
    assert xlsx.data.startswith(b"PK")


def test_comparison_reuses_harvard_key_for_both_model_families(client, tmp_path, monkeypatch):
    image = tmp_path / "scan.jpg"
    image.write_bytes(b"image")
    with client.session_transaction() as sess:
        sess["comparison_folder_path"] = str(tmp_path)
        sess["api_keys"] = {"harvard": appmod.encrypt_api_key("shared-test-key")}
    calls = []

    def transcribe(path, key, custom_prompt=None, provider=None, model=None):
        calls.append((key, provider, model))
        return json.dumps({"title": "Test", "date": "1850"})

    monkeypatch.setattr(appmod, "image_to_text", transcribe)
    models = [{"provider": "harvard", "model": "gpt-5.4"}, {"provider": "harvard_gemini", "model": "gemini-2.5-flash"}]
    response = client.post("/model-comparison/run", json={"image_paths": [str(image)], "models": models, "key_provider": "harvard"})
    assert response.status_code == 200
    assert response.get_json()["combination_count"] == 2
    assert calls == [("shared-test-key", "harvard", "gpt-5.4"), ("shared-test-key", "harvard_gemini", "gemini-2.5-flash")]


def test_comparison_caps_scan_count(client):
    response = client.post("/model-comparison/run", json={"image_paths": ["scan.jpg"] * 11})
    assert response.status_code == 400


def test_excel_upload_produces_three_downloadable_outputs(client, monkeypatch, tmp_path):
    monkeypatch.setitem(appmod.app.config, "UPLOAD_FOLDER", str(tmp_path))
    result = {"estimated_year": "1850", "all_ranges": [], "link": "https://imslp.org/wiki/Test", "source": "test", "date_estimation_analysis": {"main_table": "test", "imprint": "test", "address": "test", "plate": "test"}}
    monkeypatch.setattr(appmod, "estimate_year", lambda *args: result)
    workbook = io.BytesIO()
    pd.DataFrame([{"City": "Boston", "Publisher name": "Test", "Address": "", "Plate number": "00123"}]).to_excel(workbook, index=False)
    workbook.seek(0)
    response = client.post("/upload", data={"file": (workbook, "input.xlsx")})
    assert response.status_code == 200
    assert b"1850" in response.data
    for output in ("simple", "detailed", "cataloging"):
        download = client.get(f"/download/{output}")
        assert download.status_code == 200
        assert download.data.startswith(b"PK")
    assert pd.read_excel(tmp_path / "output_simple.xlsx")["Estimated_Year"][0] == 1850


def test_local_app_rejects_remote_or_cross_origin_requests(client):
    assert client.get("/", environ_overrides={"REMOTE_ADDR": "10.0.0.1"}).status_code == 403
    assert client.get("/", base_url="http://untrusted.example").status_code == 403
    assert client.post("/settings", headers={"Origin": "https://untrusted.example"}).status_code == 403
