const assert = require("node:assert/strict");
const { matchWake, matchStop, normalize } = require(require("path").join(__dirname, "..", "wetworks", "console", "voice.js"));
const W = "hey clair", S = ["that's all clair", "goodbye clair", "that's all", "goodbye"];
// Wake phrase, with the different ways Whisper may spell her name
for (const t of ["Hey Clair.", "Hey, Claire!", "hey clare", "Okay Clair", "Hi C.L.A.I.R.", "hey clear"]) assert.ok(matchWake(t, W), t);
assert.deepEqual(matchWake("Hey Claire, what's on the schedule today?", W), { rest: "what's on the schedule today" });
for (const t of ["", "Clear the table please", "hey there", "I declare"]) assert.equal(matchWake(t, W), null, t);
// Only when it opens the utterance: a TV or a story that mentions her doesn't wake her
assert.ok(matchWake("Um, okay, hey Clair, what's up", W));
for (const t of ["and then he said hey clair to the robot", "I told my wife hey Claire was listening"]) assert.equal(matchWake(t, W), null, t);
// Stop phrases only on their own, not inside a longer request
for (const t of ["That's all, Clair.", "Goodbye Clair!", "that's all", "Okay, goodbye.", "That’s all thanks"]) assert.ok(matchStop(t, S), t);
for (const t of ["That's all I needed about the GPU, now what about the clinics?", "Say goodbye to the old budget plan and draft a new one", ""]) assert.equal(matchStop(t, S), false, t);
assert.equal(normalize("C.L.a.i.R"), "clair");
console.log("voice phrase tests passed");
