import pytest
from app.core import security
from app.db import models as m


def auth(u):
    return {"Authorization": "Bearer " + security.create_access_token(str(u.id))}


def dependency():
    return dict(
        id="timeline-gantt-view",
        name="Time & Line",
        repository="kjgoodrick/obsidian-time-and-line-planning",
        tag="1.0.1",
        version="1.0.1",
        min_app_version="1.10.0",
        desktop_only=False,
        files={
            f: dict(size=1, sha256="a" * 64) for f in ["main.js", "manifest.json", "styles.css"]
        },
        settings={"weekStart": 1},
    )


@pytest.fixture
def team(db_session, test_user):
    share = m.Share(
        kind=m.ShareKind.FOLDER,
        path="Shared",
        managed=True,
        visibility=m.ShareVisibility.PRIVATE,
        owner_user_id=test_user.id,
    )
    member = m.User(email="member@example.com", password_hash="", is_active=True)
    stranger = m.User(email="stranger@example.com", password_hash="", is_active=True)
    db_session.add_all([share, member, stranger])
    db_session.flush()
    db_session.add(
        m.ShareMember(share_id=share.id, user_id=member.id, role=m.ShareMemberRole.EDITOR)
    )
    db_session.commit()
    return share, test_user, member, stranger


def test_owner_publish_member_read_and_cas(client, team):
    share, owner, member, stranger = team
    url = f"/v1/shared-plugins/shares/{share.id}"
    assert client.get(url, headers=auth(owner)).json()["revision"] == 0
    payload = {"expected_revision": 0, "plugins": [dependency()]}
    assert client.put(url, headers=auth(member), json=payload).status_code == 403
    assert client.get(url, headers=auth(stranger)).status_code == 403
    assert client.get(url).status_code == 401
    r = client.put(url, headers=auth(owner), json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1
    result = client.get(url, headers=auth(member)).json()
    assert result["plugins"] == payload["plugins"]
    assert result["can_manage"] is False
    assert client.put(url, headers=auth(owner), json=payload).status_code == 409
    assert (
        client.put(url, headers=auth(owner), json={"expected_revision": 1, "plugins": []}).json()[
            "revision"
        ]
        == 2
    )


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../escape"},
        {"id": "team-relay-sync"},
        {"repository": "https://evil.test/repo"},
        {"tag": "../../main"},
        {"settings": {"apiKey": "secret"}},
        {"settings": {"markers": []}},
        {"settings": {"lockOnMobile": True}},
        {"settings": {"weekStart": True}},
        {"repository": "someone/other", "settings": {"weekStart": 1}},
        {"files": {"data.json": {"sha256": "a" * 64, "size": 1}}},
    ],
)
def test_rejects_unsafe_or_personal_configuration(client, team, change):
    share, owner, _, _ = team
    p = dependency()
    p.update(change)
    assert (
        client.put(
            f"/v1/shared-plugins/shares/{share.id}",
            headers=auth(owner),
            json={"expected_revision": 0, "plugins": [p]},
        ).status_code
        == 422
    )


def test_duplicate_ids_and_removed_member(client, team, db_session):
    share, owner, member, _ = team
    url = f"/v1/shared-plugins/shares/{share.id}"
    assert (
        client.put(
            url,
            headers=auth(owner),
            json={"expected_revision": 0, "plugins": [dependency(), dependency()]},
        ).status_code
        == 422
    )
    db_session.query(m.ShareMember).filter_by(share_id=share.id, user_id=member.id).delete()
    db_session.commit()
    assert client.get(url, headers=auth(member)).status_code == 403
