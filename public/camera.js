const WORDS = {
  center: "Look straight at the camera",
  left: "← Turn your head to your left",
  right: "Turn your head to your right →",
};

const notice = document.getElementById("notice"), here = document.body.dataset.page;

async function call(url, options) {
  const r = await fetch(url, options);
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok) throw Object.assign(new Error(data.message || "Something went wrong. Try again."), { status: r.status });
  return data;
}

function post(url, body = {}) {
  const raw = body instanceof Blob;
  return call(url, {
    method: "POST",
    headers: { "Content-Type": raw ? "image/jpeg" : "application/json" },
    body: raw ? body : JSON.stringify(body),
  });
}

// The free server sleeps when unused and takes about a minute to wake.
async function whoAmI() {
  for (let tries = 0; ; tries++) {
    const slow = setTimeout(() => { notice.textContent = "Waking up the server. This can take a minute…"; }, 1500);
    try {
      return await call("/api/me");
    } catch (e) {
      if ((e.status && e.status < 500) || tries > 30) throw e;
      notice.textContent = "Waking up the server. This can take a minute…";
      await new Promise((next) => setTimeout(next, 4000));
    } finally {
      clearTimeout(slow);
    }
  }
}

// Runs the face check `start` inside `root`. Resolves when it passes, throws a message when not.
async function faceCheck(root, start, done, signal) {
  const video = root.querySelector("video"), prompt = root.querySelector(".prompt");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: "user" } });
  } catch {
    throw new Error("Cannot use the camera. Allow camera access and close other apps that use it.");
  }
  try {
    video.srcObject = stream;
    await video.play();
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    prompt.textContent = WORDS[start.prompt];
    for (;;) {
      if (signal?.aborted) throw new Error("Cancelled.");
      ctx.drawImage(video, 0, 0);
      const blob = await new Promise((next) => canvas.toBlob(next, "image/jpeg", 0.85));
      const r = await post("/api/frame/" + start.id, blob);
      if (r.status === "ok") {
        video.pause(); // hold the captured picture on screen for a moment
        prompt.textContent = done;
        await new Promise((next) => setTimeout(next, 1200));
        return;
      }
      if (r.status === "fail") throw new Error(r.message);
      const text = (!r.seen ? "No face seen. " : r.small ? "Move closer. " : "") + WORDS[r.prompt];
      if (prompt.textContent !== text) prompt.textContent = text;
      await new Promise((next) => setTimeout(next, 80));
    }
  } finally {
    stream.getTracks().forEach((t) => t.stop());
    video.srcObject = null;
  }
}

// Log in and sign up: send the details, and only if they are fine turn the camera on.
function cameraForm(id, purpose, saved, next, extra) {
  const form = document.getElementById(id), camera = document.querySelector(".camera");
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const button = form.querySelector("button");
    button.disabled = true;
    notice.textContent = "Checking…";
    try {
      const start = await post("/api/challenge", { purpose, ...extra, ...Object.fromEntries(new FormData(form)) });
      notice.textContent = "";
      form.hidden = true;
      camera.hidden = false;
      camera.querySelector(".prompt").textContent = "Allow the camera to continue.";
      await faceCheck(camera, start, saved);
      location.href = next;
    } catch (e) {
      camera.hidden = true;
      form.hidden = false;
      if (id === "login") form.elements.password.value = "";
      notice.textContent = e.message;
      button.disabled = false;
    }
  };
}

// Ask for the person's own face in the dialog, then do the action.
async function confirmed(heading, action) {
  const dialog = document.getElementById("check"), prompt = dialog.querySelector(".prompt");
  const go = document.getElementById("go"), abort = new AbortController();
  notice.textContent = "";
  document.getElementById("check-title").textContent = heading;
  prompt.textContent = "Confirm with your own face.";
  go.disabled = false;
  document.getElementById("cancel").onclick = () => dialog.close();
  dialog.onclose = () => abort.abort();
  dialog.showModal();
  try {
    await new Promise((done, fail) => {
      go.onclick = () => { go.disabled = true; done(); };
      abort.signal.onabort = () => fail(new Error("Cancelled."));
    });
    await faceCheck(dialog, await post("/api/challenge", { purpose: "stepup" }), "Confirmed.", abort.signal);
    await action();
  } catch (e) {
    notice.textContent = e.message;
  } finally {
    dialog.close();
  }
}

function row(...cells) {
  const tr = document.createElement("tr");
  for (const c of cells) tr.insertCell().append(...[c ?? ""].flat()); // text, so names cannot inject HTML
  return tr;
}

function button(text, onclick) {
  return Object.assign(document.createElement("button"), { textContent: text, onclick });
}

async function adminPage() {
  const { users, history } = await call("/api/admin");
  document.getElementById("people").replaceChildren(...users.map((u) => {
    const role = u.role === "admin" ? "user" : "admin";
    const act = (heading, url, body) => () => confirmed(heading, async () => { await post(url, body); await adminPage(); });
    return row(u.name, u.role, [
      button("Make " + role, act(`Make ${u.name} ${role === "admin" ? "an admin" : "a normal user"}`, "/api/admin/role", { id: u.id, role })), " ",
      button("Remove", act("Remove " + u.name, "/api/admin/remove", { id: u.id })),
    ]);
  }));
  document.getElementById("history").replaceChildren(...history.map((h) =>
    row(new Date(h.at * 1000).toLocaleString(), h.event, h.ok ? "ok" : "failed", h.name, h.detail)));
  document.getElementById("invite").onsubmit = (ev) => {
    ev.preventDefault();
    const role = new FormData(ev.target).get("role"), link = document.getElementById("link");
    confirmed("Invite someone", async () => {
      const { key } = await post("/api/admin/invite", { role });
      link.hidden = false;
      link.querySelector("input").value = location.origin + "/setup#key=" + key;
      link.querySelector("input").select();
    });
  };
}

(async () => {
  try {
    const { user, empty } = await whoAmI();
    notice.textContent = "";
    if (here === "login" && user) return location.replace("/");
    if ((here === "index" || here === "admin") && !user) return location.replace("/login");
    if (here === "admin" && user.role !== "admin") return location.replace("/");
    if (user) {
      document.querySelector("header").hidden = false;
      document.getElementById("admin-link").hidden = user.role !== "admin";
      document.getElementById("who").textContent = `${user.name} (${user.role})`;
      document.getElementById("logout").onclick = async () => { await post("/api/logout"); location.href = "/login"; };
    }
    if (here === "index") {
      document.getElementById("welcome").textContent = "Welcome, " + user.name;
      document.getElementById("delete-me").onclick = () => confirmed("Delete your account and face data", async () => {
        await post("/api/me/delete");
        location.href = "/login";
      });
    }
    if (here === "login") {
      document.getElementById("empty").hidden = !empty;
      document.getElementById("login").hidden = empty;
      cameraForm("login", "login", "Photo captured. It's a match.", "/");
    }
    if (here === "setup") {
      const key = new URLSearchParams(location.hash.slice(1)).get("key"); // after #, so never sent to a log
      cameraForm("setup", "register", "Face captured and saved.", "/login", { key });
    }
    if (here === "admin") await adminPage();
    document.querySelector("main").hidden = false;
    document.querySelector("[name=name]")?.focus();
  } catch (e) {
    notice.textContent = e.message;
  }
})();
