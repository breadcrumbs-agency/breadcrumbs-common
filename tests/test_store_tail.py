"""A store after a power loss: the pack can be shorter than the index says. A writer that opens the store cuts the
index records that name a lost block, so the lost blobs can be put again; a reader changes nothing."""
import os
import random
import tempfile
import unittest

from breadcrumbs.store import BLOCK_SIZE, Store, _CHECK_MAX, _REC


def files(path):
    out = {}
    for name in sorted(os.listdir(path)):
        with open(os.path.join(path, name), "rb") as f:
            out[name] = f.read()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "store")
        self.pack = os.path.join(self.path, "blobs.pack")
        self.idx = os.path.join(self.path, "blobs.idx")

    def build(self, blocks=2, each=20):
        """A store of `blocks` flushed blocks with `each` lines in each: ([lines of block 0, ...], [the pack size after
        each block])."""
        lines, ends = [], []
        store = Store(self.path)
        for b in range(blocks):
            lines.append([b"block %d line %d of the test\n" % (b, i) for i in range(each)])
            for line in lines[-1]:
                store.put(line)
            store.flush()
            ends.append(os.path.getsize(self.pack))
        store.close()
        return lines, ends


class LostTailTest(Base):
    def check_cut(self, lines, lost_blocks=1):
        """Open a writer on the damaged store: the records of the lost blocks are cut and kept, and every line of the
        store, the lost ones too, can be put and read."""
        good = [line for block in lines[:-lost_blocks] for line in block]
        lost = [line for block in lines[-lost_blocks:] for line in block]
        store = Store(self.path)
        try:
            self.assertEqual(len(store), len(good))
            for line in good:
                self.assertEqual(store.get(store.find(line)), line)
            for line in lost:
                self.assertIsNone(store.find(line))
            ids = [store.put(line) for line in lost]  # without the cut: "corrupt block at offset N"
            store.flush()
            self.assertEqual([store.get(i) for i in ids], lost)
        finally:
            store.close()
        self.assertEqual(os.path.getsize(self.idx), _REC.size * (len(good) + len(lost)))
        self.assertEqual(os.path.getsize(self.idx + ".lost"), _REC.size * len(lost))  # the cut records are kept
        again = Store(self.path, readonly=True)  # and a second open finds a whole store: nothing more is cut
        try:
            self.assertEqual(len(again), len(good) + len(lost))
            self.assertEqual(sorted(again.get(i) for i in again.ids()), sorted(good + lost))
        finally:
            again.close()
        self.assertEqual(os.path.getsize(self.idx + ".lost"), _REC.size * len(lost))

    def test_a_pack_cut_in_its_last_block(self):
        lines, ends = self.build()
        os.truncate(self.pack, ends[-1] - 7)
        self.check_cut(lines)

    def test_a_pack_cut_at_the_start_of_its_last_block(self):
        lines, ends = self.build()
        os.truncate(self.pack, ends[0])
        self.check_cut(lines)

    def test_a_pack_cut_inside_the_header_of_its_last_block(self):
        lines, ends = self.build()
        os.truncate(self.pack, ends[0] + 5)
        self.check_cut(lines)

    def test_a_last_block_of_zero_bytes(self):
        # the file has its length and the pages of the block never reached the disk
        lines, ends = self.build()
        with open(self.pack, "r+b") as f:
            f.seek(ends[0])
            f.write(bytes(ends[1] - ends[0]))
        self.check_cut(lines)

    def test_a_last_block_with_a_changed_byte(self):
        lines, ends = self.build()
        with open(self.pack, "r+b") as f:
            f.seek(ends[-1] - 3)
            byte = f.read(1)
            f.seek(ends[-1] - 3)
            f.write(bytes([byte[0] ^ 0xFF]))
        self.check_cut(lines)

    def test_two_lost_blocks(self):
        lines, ends = self.build(blocks=4)
        os.truncate(self.pack, ends[1] + 3)
        self.check_cut(lines, lost_blocks=2)

    def test_an_empty_pack_under_a_full_index(self):
        lines, _ends = self.build()
        os.truncate(self.pack, 0)
        self.check_cut(lines, lost_blocks=2)

    def test_the_new_blocks_do_not_take_the_place_of_the_lost_ones(self):
        # without the cut, a block appended at the place of the lost one gave the old records other bytes
        lines, ends = self.build()
        os.truncate(self.pack, ends[0])
        store = Store(self.path)
        other = [b"another text %d that the store never held\n" % i for i in range(20)]
        ids = [store.put(line) for line in other]
        store.close()
        store = Store(self.path, readonly=True)
        try:
            self.assertEqual([store.get(i) for i in ids], other)
            self.assertEqual(len(store), 40)
            for line in lines[1]:
                self.assertIsNone(store.find(line))
        finally:
            store.close()


class UntouchedTest(Base):
    def test_a_whole_store_is_not_cut(self):
        self.build(blocks=3)
        before = files(self.path)
        Store(self.path).close()
        self.assertEqual(files(self.path), before)
        self.assertFalse(os.path.exists(self.idx + ".lost"))

    def test_a_reader_changes_no_file_of_a_damaged_store(self):
        lines, ends = self.build()
        os.truncate(self.pack, ends[-1] - 7)
        before = files(self.path)
        store = Store(self.path, readonly=True)
        try:
            self.assertEqual(store.get(store.find(lines[0][0])), lines[0][0])
            with self.assertRaises(ValueError):
                store.get("%08x" % store._crc[len(lines[0])])  # a blob of the lost block
        finally:
            store.close()
        self.assertEqual(files(self.path), before)

    def test_a_damaged_block_in_the_middle_is_left_for_the_doctor(self):
        # only a lost tail is cut: records after the damage name good blocks, and a cut in the middle would lose them
        lines, ends = self.build(blocks=3)
        with open(self.pack, "r+b") as f:
            f.seek(ends[0] + 20)
            f.write(b"\xff\xff\xff")
        before = os.path.getsize(self.idx)
        store = Store(self.path)
        try:
            self.assertEqual(store.get(store.find(lines[2][0])), lines[2][0])
            self.assertEqual(len(store), 60)
        finally:
            store.close()
        self.assertEqual(os.path.getsize(self.idx), before)
        self.assertFalse(os.path.exists(self.idx + ".lost"))

    def test_the_blocks_of_a_writer_that_is_open_are_not_cut_by_a_second_writer(self):
        first = Store(self.path)
        a = first.put(b"flushed by the first writer\n")
        first.flush()
        b = first.put(b"still in the buffer of the first writer\n")
        second = Store(self.path)  # opens while the first has an open block
        c = second.put(b"of the second writer\n")
        second.close()
        first.close()
        store = Store(self.path, readonly=True)
        try:
            self.assertEqual([store.get(i) for i in (a, b, c)], [b"flushed by the first writer\n",
                                                                 b"still in the buffer of the first writer\n",
                                                                 b"of the second writer\n"])
        finally:
            store.close()
        self.assertFalse(os.path.exists(self.idx + ".lost"))

    def big(self, data):
        """A store whose last block is the one big blob `data`: (its id, the pack size before it, the size after)."""
        store = Store(self.path)
        store.put(b"small\n")
        store.flush()
        start = os.path.getsize(self.pack)
        big_id = store.put(data)
        store.close()
        return big_id, start, os.path.getsize(self.pack)

    def test_a_big_blob_in_a_block_of_its_own_stays(self):
        for name, data in (("repeats", bytes(range(256)) * (3 * BLOCK_SIZE // 256)),  # a small block when compressed
                           ("text", random.Random(2).randbytes(400_000).hex().encode()),  # compressed, over _CHECK_MAX
                           ("noise", random.Random(1).randbytes(600_000))):  # stored as it is, over _CHECK_MAX
            with self.subTest(name):
                self.setUp()
                big_id, start, end = self.big(data)
                self.assertGreater(end - start, _CHECK_MAX if name != "repeats" else 0)
                Store(self.path).close()
                self.assertFalse(os.path.exists(self.idx + ".lost"))
                store = Store(self.path, readonly=True)
                try:
                    self.assertEqual(store.get(big_id), data)
                finally:
                    store.close()

    def test_a_big_block_that_is_cut_or_ends_in_zero_bytes_is_lost(self):
        data = random.Random(2).randbytes(400_000).hex().encode()
        for name in ("cut", "zeros"):
            with self.subTest(name):
                self.setUp()
                big_id, start, end = self.big(data)
                if name == "cut":
                    os.truncate(self.pack, end - 100)
                else:
                    with open(self.pack, "r+b") as f:
                        f.seek(end - 8192)
                        f.write(bytes(8192))
                store = Store(self.path)
                try:
                    self.assertEqual(store.cut, 1)
                    self.assertEqual(len(store), 1)
                    self.assertEqual(store.put(data), big_id)
                    store.flush()
                    self.assertEqual(store.get(big_id), data)
                finally:
                    store.close()


class SyncTest(Base):
    def test_sync_writes_the_open_block_and_another_reader_sees_it(self):
        store = Store(self.path)
        blob_id = store.put(b"on the device\n")
        store.sync()
        other = Store(self.path, readonly=True)
        try:
            self.assertEqual(other.get(blob_id), b"on the device\n")
        finally:
            other.close()
        store.sync()  # nothing is open: it still returns
        store.close()

    def test_a_reader_has_nothing_to_sync(self):
        self.build()
        store = Store(self.path, readonly=True)
        store.sync()
        store.flush()
        store.close()


if __name__ == "__main__":
    unittest.main()
