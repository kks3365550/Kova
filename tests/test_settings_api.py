import pytest
from fastapi.testclient import TestClient
from web.app import app

@pytest.fixture
def client():
    return TestClient(app)

def test_validate_path_valid(client):
    res = client.post("/api/validate-path", json={"path": "C:\\Users"})
    assert res.status_code == 200
    data = res.json()
    assert data["exists"] is True
    assert data["is_dir"] is True
    assert data["readable"] is True
    assert data["free_bytes"] > 0

def test_validate_path_non_existent(client):
    res = client.post("/api/validate-path", json={"path": "C:\\DefinitelyNonExistentDir_999"})
    assert res.status_code == 200
    data = res.json()
    assert data["exists"] is False
    assert data["is_dir"] is False
    assert data["readable"] is False

def test_validate_path_empty_or_whitespace(client):
    res = client.post("/api/validate-path", json={"path": "   "})
    assert res.status_code == 200
    data = res.json()
    assert data["exists"] is False

def test_browse_folder_root(client):
    res = client.get("/api/browse-folder")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert len(data["items"]) > 0

def test_browse_folder_subfolder(client):
    res = client.get("/api/browse-folder?path=C:\\")
    assert res.status_code == 200
    data = res.json()
    assert data["current_path"] == "C:\\"
    assert len(data["items"]) > 0

def test_profile_crud_lifecycle(client):
    # 1. Create
    new_prof = {
        "name": "AutoFix_Test_Profile",
        "sources": ["C:\\Users\\Public"],
        "repo_dir": "D:\\MyBackup_Repository",
        "exclude_patterns": ["*.tmp"],
        "schedule_type": "daily",
        "schedule_value": "05:00",
        "auto_backup_enabled": False,
        "retention_count": 7,
        "compression_level": 3
    }
    create_res = client.post("/api/profiles", json=new_prof)
    assert create_res.status_code == 200
    created = create_res.json()
    prof_id = created["id"]
    assert prof_id.startswith("prof_")

    # 2. Update (PUT)
    created["name"] = "AutoFix_Test_Profile_Updated"
    created["retention_count"] = 12
    update_res = client.put(f"/api/profiles/{prof_id}", json=created)
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["name"] == "AutoFix_Test_Profile_Updated"
    assert updated["retention_count"] == 12

    # 3. Read
    list_res = client.get("/api/profiles")
    assert list_res.status_code == 200
    profiles = list_res.json()
    found = next((p for p in profiles if p["id"] == prof_id), None)
    assert found is not None
    assert found["name"] == "AutoFix_Test_Profile_Updated"

    # 4. Delete
    del_res = client.delete(f"/api/profiles/{prof_id}")
    assert del_res.status_code == 200
    assert del_res.json()["success"] is True

    # 5. Verify deleted
    list_res2 = client.get("/api/profiles")
    profiles2 = list_res2.json()
    assert not any(p["id"] == prof_id for p in profiles2)