// Hash-based router. Provides real browser back/forward support.
//
// Routes:
//   #/                        -> projects list
//   #/p/:folder               -> conversations list for a project
//   #/p/:folder/c/:convoId    -> messages view for a conversation

const routes = [];
let currentHandler = null;

export function defineRoute(pattern, handler) {
  routes.push({ pattern, handler });
}

function parseHash() {
  const hash = location.hash || "#/";
  return hash.startsWith("#") ? hash.slice(1) : hash;
}

async function dispatch() {
  const path = parseHash();
  for (const { pattern, handler } of routes) {
    const match = path.match(pattern);
    if (match) {
      currentHandler = handler;
      await handler(...match.slice(1));
      return;
    }
  }
  // No match -> go home
  navigate("/");
}

export function navigate(path) {
  const target = "#" + path;
  if (location.hash === target) {
    // Already here; still re-dispatch (e.g. explicit reload)
    dispatch();
  } else {
    location.hash = target;
  }
}

export function initRouter() {
  window.addEventListener("hashchange", dispatch);
  dispatch();
}
