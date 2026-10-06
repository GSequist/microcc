#!/usr/bin/env node
/** Re-stamps free.js after the skill's maintainer edits it: `node stamp.mjs`. A figure never needs this. */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const file = fileURLToPath(new URL("./free.js", import.meta.url));
let s = readFileSync(file, "utf8").replace(/\r\n/g, "\n").trimEnd();
const hash = createHash("sha256").update(s.slice(s.indexOf("\n") + 1) + "\n").digest("hex");
s = s.replace(/sha256:[0-9a-f]{64}/, `sha256:${hash}`);
writeFileSync(file, s + "\n");
console.log(hash);
