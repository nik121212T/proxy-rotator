from pathlib import Path

import pytest

from app.browser_storage import delete_engine, engine_folders, installed_size


def test_delete_engine_preserves_other_engines_and_shared_dependencies(tmp_path):
    for name in ('chromium-123', 'chromium_headless_shell-123', 'firefox-4', 'ffmpeg-9', 'chromium-122'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'binary').write_bytes(b'12345')
    folders = engine_folders('chromium', tmp_path / 'chromium-123/binary')
    assert installed_size(folders) == 10
    delete_engine(folders)
    assert installed_size(folders) == 0
    for name in ('firefox-4', 'ffmpeg-9', 'chromium-122'):
        assert (tmp_path / name / 'binary').exists()


def test_unknown_paths_and_symlinks_are_never_deleted(tmp_path):
    with pytest.raises(ValueError):
        engine_folders('chromium', Path('/Applications/Google Chrome.app/browser'))
    outside = tmp_path / 'personal'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (tmp_path / 'firefox-4').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        engine_folders('firefox', tmp_path / 'firefox-4/browser')
    assert (outside / 'keep').read_text() == 'keep'
