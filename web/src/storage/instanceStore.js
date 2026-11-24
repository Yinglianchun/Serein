import { readLocalPreference, storeLocalPreference } from "./awakeStore.js";
import { settingsResponseError } from "./settingsError.js";

export function identityName(role) {
  return role === "user"
    ? readLocalPreference("serein.awake.name.user", "User")
    : readLocalPreference("serein.awake.name.assistant", "AI");
}

let pendingRead;

export async function instanceSettings(changes) {
  if (changes) {
    pendingRead = undefined;
    try {
      return await requestSettings(changes);
    } finally {
      pendingRead = undefined;
    }
  }
  const read = pendingRead ??= requestSettings();
  try {
    return await read;
  } finally {
    if (pendingRead === read) pendingRead = undefined;
  }
}

async function requestSettings(changes) {
  const response = await fetch("/__serein/settings", {
    method: changes ? "PATCH" : "GET",
    headers: changes ? { "Content-Type": "application/json" } : {},
    ...(changes ? { body: JSON.stringify(changes) } : {}),
  });
  if (!response.ok) {
    throw await settingsResponseError(response);
  }
  const result = await response.json();
  storeLocalPreference("serein.awake.name.user", result.identity.user_name);
  storeLocalPreference("serein.awake.name.assistant", result.identity.ai_name);
  storeLocalPreference("serein.awake.meetingDate", result.identity.meeting_date ?? "");
  window.dispatchEvent(new CustomEvent('serein:features',{detail:result.features}));
  if(changes)window.dispatchEvent(new CustomEvent('serein:settings-saved',{detail:result}));
  return result;
}
