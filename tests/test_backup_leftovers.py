"""An interrupted backup, restore or check leaves nothing behind (#509).

Each works in a folder beside the database that holds an unencrypted copy of it, and a backup writes its archive to a hidden ``.partial`` file
first. Both were removed only if the process reached its clean-up; after a kill they stayed for good, on the disk whose filling was part of the
production incident. The supervisor now removes them at start, leaving alone any that another process is still using."""
from __future__ import annotations

import io
import os
import signal
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

from app import backup
from app.store import database_path
from tests.test_backups import PASSPHRASE, BackupCase


def hold(path: Path) -> subprocess.Popen:
    """Another process that holds ``path`` the way a running backup does, until it is ended. Returns once the lock is taken."""
    script = textwrap.dedent(f"""
        import fcntl, os, sys, time
        handle = os.open({str(path)!r}, os.O_RDONLY)
        fcntl.flock(handle, fcntl.LOCK_EX)
        print("held", flush=True)
        time.sleep(60)
    """)
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "held"
    return process


class LeftoverTests(BackupCase):
    def leftovers(self) -> list[Path]:
        """What a backup, a restore and a check killed half way leave: three work folders, each with a copy of the database, and a partial archive."""
        made = []
        for prefix in backup.WORK_PREFIXES:
            folder = database_path().parent / f"{prefix}abc123"
            folder.mkdir()
            (folder / "nvr.sqlite3").write_bytes(b"\0" * 300_000)
            made.append(folder)
        self.backups.mkdir(exist_ok=True)
        partial = self.backups / ".nvr-config-20261123T010203000000Z-scheduled.nvrbackup.partial"
        partial.write_bytes(b"\0" * 200_000)
        return [*made, partial]

    def test_what_an_interrupted_backup_restore_or_check_left_is_removed_and_the_room_it_freed_is_logged(self):
        leftovers = self.leftovers()
        kept = [database_path(), self.backups / "nvr-config-20261123T000000000000Z-manual.nvrbackup", database_path().parent / ".failover-elsewhere"]
        kept[1].write_bytes(b"a backup")
        kept[2].mkdir()  # The failover sync's own folders are its own to clear (failover.remove_leftovers)
        with self.assertLogs("nvr.backup", "WARNING") as logged:
            freed = backup.remove_leftovers()
        self.assertEqual(freed, 3 * 300_000 + 200_000)
        self.assertEqual([path for path in leftovers if path.exists()], [])
        self.assertTrue(all(path.exists() for path in kept))
        self.assertIn("freeing 1 MB", logged.output[0])
        self.assertIn(".backup-abc123", logged.output[0])

    def test_nothing_to_remove_says_nothing(self):
        with self.assertNoLogs("nvr.backup", "WARNING"):
            self.assertEqual(backup.remove_leftovers(), 0)

    def test_a_backup_still_running_in_another_process_is_left_alone(self):
        folder, _, _, partial = self.leftovers()
        holders = [hold(folder), hold(partial)]
        try:
            backup.remove_leftovers()
            self.assertTrue(folder.exists() and (folder / "nvr.sqlite3").exists())
            self.assertTrue(partial.exists())
        finally:
            for process in holders:
                process.kill()
                process.wait()
        backup.remove_leftovers()  # Its process was killed: the lock went with it, and now the folder goes
        self.assertFalse(folder.exists())
        self.assertFalse(partial.exists())

    def test_a_backup_holds_its_folder_and_its_archive_while_it_runs_and_leaves_nothing(self):
        seen: dict[str, bool] = {}
        real = backup.tarfile.open

        def looking(*args, **kwargs):
            """While the archive is being written, a clean-up in another process must find both the folder and the archive in use."""
            work = next(database_path().parent.glob(".backup-*"))
            partial = next(self.backups.glob(".nvr-config-*.partial"))
            checker = textwrap.dedent(f"""
                import fcntl, os, sys
                for path in ({str(work)!r}, {str(partial)!r}):
                    handle = os.open(path, os.O_RDONLY)
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        print("free")
                    except BlockingIOError:
                        print("held")
            """)
            answer = subprocess.run([sys.executable, "-c", checker], capture_output=True, text=True, check=True).stdout.split()
            seen["folder"], seen["archive"] = answer[0] == "held", answer[1] == "held"
            return real(*args, **kwargs)

        with self._patch_tar(looking):
            backup.create_backup("manual")
        self.assertEqual(seen, {"folder": True, "archive": True})
        self.assertEqual(list(database_path().parent.glob(".backup-*")), [])
        self.assertEqual(list(self.backups.glob(".*.partial")), [])

    def _patch_tar(self, replacement):
        from unittest.mock import patch
        return patch.object(backup.tarfile, "open", replacement)

    def test_a_check_killed_half_way_leaves_a_folder_the_next_start_removes(self):
        archive = self.backups / str(backup.create_backup("manual")["file_name"])
        script = textwrap.dedent(f"""
            import os, signal, sys
            from pathlib import Path
            from app import backup
            real = backup._unpack_checked
            def then_die(archive, secret, work_dir):
                real(archive, secret, work_dir)
                print(work_dir, flush=True)
                os.kill(os.getpid(), signal.SIGKILL)  # Killed with the unencrypted copy unpacked
            backup._unpack_checked = then_die
            backup.verify(Path({str(archive)!r}))
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=os.environ.copy(), cwd=Path(__file__).parent.parent)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        left = Path(result.stdout.strip())
        self.assertTrue((left / "nvr.sqlite3").exists())  # What the ticket found: an unencrypted copy, never removed
        backup.remove_leftovers()
        self.assertFalse(left.exists())


class CostTests(BackupCase):
    def test_a_header_asking_for_a_cost_this_machine_will_not_spend_is_refused_plainly(self):
        sealed = io.BytesIO()
        backup.encrypt_stream(io.BytesIO(b"configuration"), sealed, PASSPHRASE)
        data = bytearray(sealed.getvalue())
        at = len(backup.MAGIC) + 16
        data[at:at + 12] = struct.pack(">III", 2**20, 16, 1)  # Within the old bounds, but 2 GB of memory: scrypt raised ValueError
        with self.assertRaises(backup.BackupError) as raised:
            backup.decrypt_stream(io.BytesIO(bytes(data)), io.BytesIO(), PASSPHRASE)
        self.assertIn("not a valid NVR configuration backup", str(raised.exception))

    def test_the_offline_tool_says_so_rather_than_fail_with_a_trace(self):
        self.backups.mkdir(exist_ok=True)
        crafted = self.backups / "crafted.nvrbackup"
        crafted.write_bytes(backup.MAGIC + b"\0" * 16 + struct.pack(">III", 2**20, 16, 1) + b"\0" * 8 + b"\0" * 64)
        result = subprocess.run([sys.executable, "-m", "app.backup", "verify", str(crafted)], capture_output=True, text=True, env=os.environ.copy(),
                                cwd=Path(__file__).parent.parent)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("error: This is not a valid NVR configuration backup", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
