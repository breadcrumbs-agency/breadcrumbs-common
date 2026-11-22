"""atomic.write gives a file its content in one step, and atomic.locked lets one holder in at a time."""
import os
import stat
import tempfile
import threading
import unittest

from breadcrumbs import atomic


class WriteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "file.json")

    def test_it_writes_bytes_and_text_and_leaves_no_temporary_file(self):
        atomic.write(self.path, b"one\n")
        atomic.write(self.path, "zwei \u00e4\n")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), "zwei \u00e4\n".encode("utf-8"))
        self.assertEqual(os.listdir(self.dir), ["file.json"])

    def test_a_temporary_name_is_in_the_same_folder_hidden_and_never_given_twice(self):
        names = {atomic.temp_name(self.path) for _ in range(100)}
        self.assertEqual(len(names), 100)
        for name in names:
            self.assertEqual(os.path.dirname(name), self.dir)
            self.assertTrue(os.path.basename(name).startswith(".file.json."))
            self.assertTrue(name.endswith(".tmp"))

    def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(self):
        atomic.write(self.path, b"old\n")
        with self.assertRaises(TypeError):
            atomic.write(self.path, 12345)  # no bytes and no text
        os.mkdir(os.path.join(self.dir, "folder"))
        with self.assertRaises(OSError):
            atomic.write(os.path.join(self.dir, "folder"), b"x")  # a folder is in the way of the rename
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"old\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["file.json", "folder"])
        self.assertEqual(os.listdir(os.path.join(self.dir, "folder")), [])

    def test_the_mode_is_the_one_that_was_asked_for(self):
        atomic.write(self.path, b"secret\n", mode=0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_sync_writes_the_same_bytes(self):
        atomic.write(self.path, b"kept\n", sync=True)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"kept\n")


@unittest.skipIf(atomic.fcntl is None, "no flock on this platform")
class LockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def count(self, lock, threads=8, rounds=200):
        """A read, then a write of one number, by many threads under the lock: the count that was kept."""
        box = [0]

        def work():
            for _ in range(rounds):
                with atomic.locked(lock):
                    n = box[0]
                    box[0] = n + 1

        ts = [threading.Thread(target=work) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return box[0]

    def test_the_lock_of_a_folder_makes_no_file(self):
        self.assertEqual(self.count(self.dir), 1600)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_lock_file_is_made_when_it_is_missing(self):
        lock = os.path.join(self.dir, "x.lock")
        self.assertEqual(self.count(lock), 1600)
        self.assertEqual(os.listdir(self.dir), ["x.lock"])

    def test_a_thread_that_holds_the_lock_takes_it_again_without_waiting(self):
        done = []

        def work():
            with atomic.locked(self.dir):
                with atomic.locked(self.dir):  # a block inside a block of the same lock
                    with atomic.locked(os.path.join(self.dir, ".")):  # the same folder by another name
                        done.append(1)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(10)
        self.assertEqual(done, [1])

    def test_a_second_thread_waits_until_the_first_lets_go(self):
        order, inside, release = [], threading.Event(), threading.Event()

        def first():
            with atomic.locked(self.dir):
                inside.set()
                release.wait(10)
                order.append("first")

        def second():
            inside.wait(10)
            with atomic.locked(self.dir):
                order.append("second")

        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start()
        b.start()
        inside.wait(10)
        b.join(0.2)  # the second is waiting: it has added nothing
        self.assertEqual(order, [])
        release.set()
        a.join(10)
        b.join(10)
        self.assertEqual(order, ["first", "second"])

    def test_the_lock_is_free_again_after_an_error_in_the_block(self):
        with self.assertRaises(RuntimeError):
            with atomic.locked(self.dir):
                raise RuntimeError("in the block")
        self.assertEqual(self.count(self.dir, threads=2, rounds=10), 20)


if __name__ == "__main__":
    unittest.main()
