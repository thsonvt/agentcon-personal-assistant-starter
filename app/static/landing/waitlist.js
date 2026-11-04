"use strict";

const form = document.querySelector("#waitlist");
const entry = document.querySelector("#waitlist-entry");
const emailInput = document.querySelector("#waitlist-email");
const feedback = document.querySelector("#waitlist-feedback");
const dialog = document.querySelector("#waitlist-survey");
const survey = document.querySelector("#survey-form");
const progress = document.querySelector("#survey-progress");
const back = document.querySelector("#survey-back");
const next = document.querySelector("#survey-next");
const skip = document.querySelector("#survey-skip");
const close = document.querySelector("#survey-close");
const surveyFeedback = document.querySelector("#survey-feedback");
const firstName = document.querySelector("#survey-first-name");
const tools = Array.from(document.querySelectorAll("input[name='schedulingTools']"));
const toolsOther = document.querySelector("#survey-tools-other");
const challenge = document.querySelector("#survey-challenge");
const wish = document.querySelector("#survey-wish");
const steps = Array.from(document.querySelectorAll("[data-survey-step]"));
let step = 0;
let submitting = false;
let complete = false;
let token = "";
let widgetId;
let config;
let securityStarted = false;
let securityResetPending = false;
let savedOverflow = "";
let unsupportedDomains = ["yahoo.com", "aol.com", "icloud.com", "outlook.com", "hotmail.com", "live.com"];
const emailErrors = {
  invalid_email: "Please go back to your email and enter a complete address, such as you@gmail.com.",
  unsupported_email_provider: "Pippa currently supports personal Gmail and Google Workspace work email only. Please use a supported address.",
  workspace_unverified: "We couldn't confirm Google Workspace for this domain. Close this form to use a personal Gmail address, or contact Son for help. Your answers will stay here.",
  email_check_unavailable: "We couldn't check your work email right now. Please try again shortly.",
  invalid_survey: "Please check your answers. Your first name should be 80 characters or fewer, and each written answer 600 or fewer.",
};
function message(element, state, text) {
  element.dataset.state = state;
  element.textContent = text;
}
function updateButton() {
  next.disabled = submitting || (step === 3 && !token);
  next.textContent = submitting ? "Saving…" : step === 3 ? "Join Pippa's early access →" : "Continue →";
  back.disabled = submitting;
  close.disabled = submitting;
  skip.disabled = submitting;
}
function clearToken() { token = ""; updateButton(); }
function securityError(text) { securityResetPending = false; clearToken(); message(surveyFeedback, "error", text); }
function resetSecurity() {
  if (submitting || complete || securityResetPending || token || widgetId === undefined || !window.turnstile) return;
  securityResetPending = true;
  try { window.turnstile.reset(widgetId); }
  catch { securityError("The security check couldn't restart. Close and reopen this form to retry."); }
}
async function loadConfig() {
  try {
    const response = await fetch("/api/waitlist/config", {signal: AbortSignal.timeout(10000)});
    const result = await response.json();
    if (!response.ok || !result.enabled || !result.siteKey) throw new Error("Unavailable");
    config = result;
    if (Array.isArray(result.unsupportedDomains)) unsupportedDomains = [...new Set([...unsupportedDomains, ...result.unsupportedDomains.filter(value => typeof value === "string")])];
    if (step === 3 && dialog.open) prepareSecurity();
  } catch {
    config = false;
    if (step === 3 && dialog.open) securityError("Signup is temporarily unavailable. Your answers are still here; please try again later.");
  }
}
function prepareSecurity() {
  if (!config) {
    if (config === false) securityError("Signup is temporarily unavailable. Your answers are still here; please try again later.");
    return;
  }
  if (securityStarted) { resetSecurity(); return; }
  securityStarted = true;
  window.pippaTurnstileReady = () => {
    try {
      widgetId = window.turnstile.render("#waitlist-challenge", {
        sitekey: config.siteKey, action: "waitlist", size: "flexible", "response-field": false,
        callback: value => {
          securityResetPending = false;
          // A widget callback during a pending request cannot authorize another submit.
          if (submitting || complete) return;
          token = value;
          updateButton();
          if (surveyFeedback.dataset.state === "challenge") message(surveyFeedback, "ready", "");
        },
        "expired-callback": () => { securityResetPending = false; clearToken(); message(surveyFeedback, "challenge", "Please complete the security check again."); },
        "error-callback": () => securityError("The security check couldn't load. Check your connection, then close and reopen this form to retry."),
      });
    } catch { securityError("The security check couldn't load. Close and reopen this form to retry."); securityStarted = false; }
  };
  if (window.turnstile) { window.pippaTurnstileReady(); return; }
  const script = document.createElement("script");
  script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=pippaTurnstileReady&render=explicit";
  script.async = true;
  script.onerror = () => { securityStarted = false; securityError("The security check couldn't load. Close and reopen this form to retry."); };
  document.head.appendChild(script);
}
function showStep(focus = true) {
  steps.forEach((element, index) => { element.hidden = index !== step; });
  progress.textContent = "Question " + (step + 1) + " of 4";
  back.hidden = step === 0;
  skip.hidden = step === 0 || step === 3;
  updateButton();
  if (step === 3) prepareSecurity();
  if (focus) [firstName, tools[0], challenge, wish][step].focus();
}
function validateStep() {
  if (!firstName.value.trim() || firstName.value.trim().length > 80) {
    step = 0; showStep(); message(surveyFeedback, "error", "Please enter your first name (80 characters or fewer)."); return false;
  }
  if (toolsOther.value.trim().length > 120 || challenge.value.trim().length > 600 || wish.value.trim().length > 600) {
    message(surveyFeedback, "error", "Please keep other tools to 120 characters and written answers to 600 characters."); return false;
  }
  return true;
}
function advance() {
  if (!validateStep()) return;
  message(surveyFeedback, "ready", "");
  step = Math.min(3, step + 1); showStep();
}
form.addEventListener("input", () => {
  if (feedback.dataset.state === "eligibility") message(feedback, "ready", "");
});
form.addEventListener("submit", event => {
  event.preventDefault();
  if (submitting || !form.reportValidity()) return;
  const domain = emailInput.value.trim().split("@").pop().toLowerCase();
  if (unsupportedDomains.some(blocked => domain === blocked || domain.endsWith("." + blocked))) {
    message(feedback, "eligibility", emailErrors.unsupported_email_provider); return;
  }
  if (complete) {
    survey.reset(); complete = false; step = 0; clearToken();
    document.querySelector("#survey-fields").hidden = false;
    document.querySelector("#survey-success").hidden = true;
    document.querySelector("#survey-other-wrap").hidden = true;
  }
  message(feedback, "ready", "");
  savedOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  dialog.showModal(); showStep();
  if (config === false) loadConfig();
});
close.addEventListener("click", () => { if (!submitting) dialog.close(); });
document.querySelector("#survey-done").addEventListener("click", () => dialog.close());
dialog.addEventListener("cancel", event => { if (submitting) event.preventDefault(); });
dialog.addEventListener("close", () => { document.body.style.overflow = savedOverflow; entry.focus(); });
back.addEventListener("click", () => {
  if (submitting) return;
  step = Math.max(0, step - 1); message(surveyFeedback, "ready", ""); showStep();
});
skip.addEventListener("click", () => { if (!submitting) advance(); });
tools.forEach(tool => tool.addEventListener("change", () => {
  document.querySelector("#survey-other-wrap").hidden = !tools.some(item => item.value === "other" && item.checked);
}));
survey.addEventListener("submit", async event => {
  event.preventDefault();
  if (submitting || complete) return;
  if (step < 3) { advance(); return; }
  if (!validateStep()) return;
  if (!token) { message(surveyFeedback, "challenge", "Please complete the security check before joining."); return; }
  const fields = new FormData(form);
  const selected = tools.filter(item => item.checked).map(item => item.value);
  const payload = new URLSearchParams({
    email: emailInput.value.trim(), website: String(fields.get("website") || ""), "cf-turnstile-response": token,
    surveyVersion: "1", firstName: firstName.value.trim(), schedulingTools: JSON.stringify(selected),
    schedulingToolsOther: selected.includes("other") ? toolsOther.value.trim() : "",
    schedulingChallenge: challenge.value.trim(), pippaWish: wish.value.trim(),
  });
  token = ""; submitting = true; updateButton();
  survey.setAttribute("aria-busy", "true"); message(surveyFeedback, "pending", "Saving your signup…");
  try {
    const response = await fetch(form.action, {method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: payload.toString(), signal: AbortSignal.timeout(20000)});
    if (response.status === 429) { message(surveyFeedback, "error", "Too many attempts. Please wait a while before trying again."); return; }
    const result = await response.json();
    if (!response.ok || result.success !== true) {
      message(surveyFeedback, "error", emailErrors[result.code] || (response.status === 403 ? "The security check expired or failed. Please complete it again." : "We couldn't confirm your signup. Please try again later.")); return;
    }
    complete = true;
    document.querySelector("#survey-fields").hidden = true;
    document.querySelector("#survey-success").hidden = false;
    const title = document.querySelector("#survey-success-title");
    title.textContent = "Thanks, " + firstName.value.trim() + ". You're on the list.";
    title.focus();
    message(surveyFeedback, "success", "");
    message(feedback, "success", "Your signup is saved. We'll email you with next steps when your invitation is ready.");
  } catch { message(surveyFeedback, "error", "We couldn't confirm your signup. Your answers are still here. Check your connection and try again."); }
  finally {
    submitting = false; survey.removeAttribute("aria-busy"); clearToken();
    // A successful flow resets on its next final step, when callbacks can be accepted.
    resetSecurity();
  }
});
entry.disabled = false; // Browsing the questions does not depend on Cloudflare loading.
loadConfig();
