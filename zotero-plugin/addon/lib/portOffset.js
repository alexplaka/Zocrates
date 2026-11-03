// Copyright 2026 Google LLC
// Licensed under the Apache License, Version 2.0

// ZocratesPortOffset — deterministic backend port derivation for the Zotero
// frontend (loaded via Services.scriptloader.loadSubScript into the
// Zotero.Zocrates namespace) and the Node.js parity test suite (require()).
//
// The formula is PINNED to app/config.py::compute_deterministic_port and must
// stay byte-for-byte identical on both sides:
//
//   8765 + (first 8 bytes of SHA-256(UTF-8(path)) as big-endian uint64 % 100)
//
// i.e. always within the 8765..8864 range. The pinned cross-language parity
// vector ("/home/alex/Zotero" -> 8832) is asserted in
// tests/unit/test_port_allocation.py and tests/js/test_port_parity.js.

var ZocratesPortOffset = {
  BASE_PORT: 8765,
  SPAN: 100,
  DATA_SUBDIR: "zocrates-data",

  // Join a base Zotero data directory with the zocrates-data subdir using the
  // separator style detected from the base path itself (Zotero.DataDirectory.dir
  // uses backslashes on Windows and forward slashes elsewhere), normalized
  // without a trailing slash. This keeps the hashed string byte-identical to
  // str(Path(base) / "zocrates-data") in Python on every platform.
  computeZocratesDataDir(baseDir) {
    const base = String(baseDir || "").replace(/[\\/]+$/, "");
    const sep = base.indexOf("\\") !== -1 ? "\\" : "/";
    return base + sep + this.DATA_SUBDIR;
  },

  // Pure (async) hash function: an absolute directory path -> backend port.
  // Hashes exactly the UTF-8 bytes of the normalized path (no trailing slash),
  // reads the first 8 digest bytes as a big-endian unsigned integer, applies
  // mod 100, and adds the base port.
  async computePortFromPath(dataDirPath) {
    const normalized = String(dataDirPath || "").replace(/[\\/]+$/, "");
    const subtle = this._getSubtle();
    if (!subtle) throw new Error("WebCrypto subtle is unavailable");
    const digest = await subtle.digest("SHA-256", new TextEncoder().encode(normalized));
    const view = new DataView(digest);
    // DataView reads are big-endian by default; take the first 8 bytes.
    const head = view.getBigUint64(0);
    return this.BASE_PORT + Number(head % BigInt(this.SPAN));
  },

  _getSubtle() {
    try {
      if (typeof crypto !== "undefined" && crypto.subtle) return crypto.subtle;
    } catch (e) {}
    // Node.js (< 19) fallback for the parity test suite.
    try {
      if (typeof require !== "undefined") return require("crypto").webcrypto.subtle;
    } catch (e) {}
    return null;
  },
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = ZocratesPortOffset;
}
