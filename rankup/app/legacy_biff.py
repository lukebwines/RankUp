"""Lettore prudente BIFF8/OLE per esportazioni PICA .xls legacy.

Fallback senza dipendenze aggiuntive quando xlrd non riesce a leggere
un file autentico .xls. Non esegue macro o formule.
"""
from __future__ import annotations

import struct

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
END = 0xFFFFFFFE
MAX_STREAM = 12 * 1024 * 1024


def read_rows(content: bytes):
    if content[:8] != MAGIC or len(content) < 512:
        raise ValueError("Il file non è un Excel .xls OLE valido")
    word = lambda o: struct.unpack_from("<H", content, o)[0]
    dword = lambda o: struct.unpack_from("<I", content, o)[0]
    shift = word(30)
    if shift not in (9, 12):
        raise ValueError("Formato .xls OLE non supportato")
    bs = 1 << shift
    nfat = dword(44)
    if nfat > 4096 or len(content) > MAX_STREAM:
        raise ValueError("Dimensioni del file .xls non supportate")
    sid_limit = (len(content) - 512) // bs
    fat_sids = [dword(76 + 4 * i) for i in range(109)
                if dword(76 + 4 * i) < 0xFFFFFFFA]
    nxt = dword(68)
    for _ in range(dword(72)):
        if nxt >= sid_limit:
            raise ValueError("Catena DIFAT non valida")
        values = struct.unpack(
            "<%dI" % (bs // 4),
            content[512 + nxt * bs:512 + (nxt + 1) * bs]
        )
        fat_sids.extend(x for x in values[:-1] if x < 0xFFFFFFFA)
        nxt = values[-1]
    fat = []
    for sid in fat_sids[:nfat]:
        if sid >= sid_limit:
            raise ValueError("Tabella FAT non valida")
        fat.extend(struct.unpack_from("<%dI" % (bs // 4), content, 512 + sid * bs))

    def read_chain(sid):
        out = bytearray()
        seen = set()
        while sid != END:
            if sid >= sid_limit or sid in seen or sid >= len(fat) or len(out) > MAX_STREAM:
                raise ValueError("Catena di settori OLE non valida")
            seen.add(sid)
            out.extend(content[512 + sid * bs:512 + (sid + 1) * bs])
            sid = fat[sid]
        return bytes(out)

    directory = read_chain(dword(48))
    workbook = None
    for pos in range(0, len(directory), 128):
        entry = directory[pos:pos + 128]
        if len(entry) < 128:
            break
        nch = struct.unpack_from("<H", entry, 64)[0]
        if nch < 2 or nch > 64:
            continue
        name = entry[:nch - 2].decode("utf-16le", "replace")
        if entry[66] == 2 and name in ("Workbook", "Book"):
            start = struct.unpack_from("<I", entry, 116)[0]
            size = struct.unpack_from("<Q", entry, 120)[0]
            if size < 4096 or size > MAX_STREAM:
                raise ValueError("Stream Workbook .xls non supportato")
            workbook = read_chain(start)[:size]
            break
    if workbook is None:
        raise ValueError("Stream Workbook non trovato nel file .xls")

    records = []
    pos = 0
    while pos + 4 <= len(workbook):
        kind, size = struct.unpack_from("<HH", workbook, pos)
        if pos + 4 + size > len(workbook):
            raise ValueError("Record Excel danneggiato")
        records.append((pos, kind, workbook[pos + 4:pos + 4 + size]))
        pos += 4 + size
    bounds = next((struct.unpack_from("<I", d)[0] for _, kind, d in records
                   if kind == 0x85 and len(d) >= 4), None)
    if bounds is None:
        raise ValueError("Foglio di lavoro non trovato")
    i = next((j for j, r in enumerate(records)
              if r[0] == bounds and r[1] == 0x809), None)
    if i is None:
        raise ValueError("Inizio del foglio non trovato")

    sst_idx = next((j for j, r in enumerate(records) if r[1] == 0xFC), None)
    if sst_idx is None:
        raise ValueError("Tabella stringhe non trovata")
    chunks = [records[sst_idx][2]]
    for _, kind, data in records[sst_idx + 1:]:
        if kind != 0x3C:
            break
        chunks.append(data)

    class Cursor:
        def __init__(self):
            self.chunk = 0
            self.offset = 0

        def read(self, n):
            parts = []
            while n:
                if self.chunk >= len(chunks):
                    raise ValueError("Tabella stringhe troncata")
                c = chunks[self.chunk]
                avail = len(c) - self.offset
                if not avail:
                    self.chunk += 1
                    self.offset = 0
                    continue
                k = min(n, avail)
                parts.append(c[self.offset:self.offset + k])
                self.offset += k
                n -= k
            return b"".join(parts)

        def chars(self, count, wide):
            parts = []
            while count:
                if self.chunk >= len(chunks):
                    raise ValueError("Testo Excel troncato")
                c = chunks[self.chunk]
                unit = 2 if wide else 1
                available = (len(c) - self.offset) // unit
                if not available:
                    self.chunk += 1
                    self.offset = 0
                    if self.chunk >= len(chunks):
                        raise ValueError("Testo Excel troncato")
                    wide = bool(self.read(1)[0] & 1)
                    continue
                k = min(available, count)
                block = c[self.offset:self.offset + k * unit]
                parts.append(block.decode("utf-16le" if wide else "latin1", "replace"))
                self.offset += k * unit
                count -= k
            return "".join(parts)

    stream = Cursor()
    total, unique = struct.unpack("<II", stream.read(8))
    if unique > 200000:
        raise ValueError("Troppi valori testuali nel file")
    strings = []
    for _ in range(unique):
        count, flags = struct.unpack("<HB", stream.read(3))
        rich = struct.unpack("<H", stream.read(2))[0] if flags & 8 else 0
        ext = struct.unpack("<I", stream.read(4))[0] if flags & 4 else 0
        strings.append(stream.chars(count, bool(flags & 1)))
        if rich:
            stream.read(rich * 4)
        if ext:
            stream.read(ext)

    rows = {}
    maxrow = maxcol = -1

    def add(row, col, value):
        nonlocal maxrow, maxcol
        if row >= 20000 or col >= 300:
            raise ValueError("Troppe righe o colonne nel file PICA")
        rows.setdefault(row, {})[col] = value
        maxrow, maxcol = max(maxrow, row), max(maxcol, col)

    for _, kind, data in records[i + 1:]:
        if kind == 0x0A:
            break
        if kind == 0x0200 and len(data) >= 12:
            lastrow = struct.unpack_from("<I", data, 4)[0]
            lastcol = struct.unpack_from("<H", data, 10)[0]
            if lastrow > 20000 or lastcol > 300:
                raise ValueError("Dimensioni PICA oltre i limiti")
        elif kind == 0xFD and len(data) >= 10:
            row, col, xf, idx = struct.unpack_from("<HHHI", data)
            if idx >= len(strings):
                raise ValueError("Indice stringa Excel non valido")
            add(row, col, strings[idx])
        elif kind == 0x203 and len(data) >= 14:
            row, col = struct.unpack_from("<HH", data)
            add(row, col, struct.unpack_from("<d", data, 6)[0])
        elif kind == 0x27E and len(data) >= 10:
            row, col = struct.unpack_from("<HH", data)
            val = struct.unpack_from("<I", data, 6)[0]
            if val & 2:
                number = val >> 2
                if number & 0x20000000:
                    number -= 0x40000000
                number = number / 100 if val & 1 else number
            else:
                number = struct.unpack("<d", struct.pack("<II", 0, val & 0xFFFFFFFC))[0]
                if val & 1:
                    number /= 100
            add(row, col, number)
        elif kind == 0x204 and len(data) >= 9:
            row, col = struct.unpack_from("<HH", data)
            nch = struct.unpack_from("<H", data, 6)[0]
            flags = data[8]
            value = data[9:9 + nch * (2 if flags & 1 else 1)].decode(
                "utf-16le" if flags & 1 else "latin1", "replace"
            )
            add(row, col, value)

    if maxrow < 1:
        raise ValueError("Il foglio Excel non contiene dati PICA")
    for row in range(maxrow + 1):
        values = rows.get(row, {})
        yield [values.get(col, "") for col in range(maxcol + 1)]
