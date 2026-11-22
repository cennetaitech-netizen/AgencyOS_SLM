import { StrictMode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { AdminPage, AssistantPanel, BillingPage, ChatApiClient } from "../src";

// Local development only: identities come from X-Dev-* headers (the chat service must run with CHAT_AUTH_PROVIDER=dev).
const saved = (k: string, d: string) => localStorage.getItem(k) ?? d;

function Demo() {
  const [org, setOrg] = useState(saved("aos.org", "11111111-1111-1111-1111-111111111111"));
  const [user, setUser] = useState(saved("aos.user", "22222222-2222-2222-2222-222222222222"));
  const [roles, setRoles] = useState(saved("aos.roles", "planner,org_admin"));
  const [page, setPage] = useState<"chat" | "billing" | "admin">("chat");
  const client = useMemo(() => {
    localStorage.setItem("aos.org", org);
    localStorage.setItem("aos.user", user);
    localStorage.setItem("aos.roles", roles);
    return new ChatApiClient({
      baseUrl: "/api",
      getHeaders: () => ({ "X-Dev-Org-Id": org, "X-Dev-User-Id": user, "X-Dev-Roles": roles, "X-Dev-Name": "Priya" }),
    });
  }, [org, user, roles]);

  return (
    <div className="demo">
      <nav>
        <button onClick={() => setPage("chat")}>Chat</button>
        <button onClick={() => setPage("billing")}>Billing</button>
        <button onClick={() => setPage("admin")}>Admin</button>
        <label>Org <input value={org} onChange={(e) => setOrg(e.target.value)} /></label>
        <label>User <input value={user} onChange={(e) => setUser(e.target.value)} /></label>
        <label>Roles <input value={roles} onChange={(e) => setRoles(e.target.value)} /></label>
      </nav>
      {page === "chat" ? (
        <AssistantPanel key={org + user} client={client} onBuyTokens={() => setPage("billing")} />
      ) : page === "billing" ? (
        <BillingPage client={client} onCheckout={(url) => alert(`Checkout: ${url}`)} />
      ) : (
        <AdminPage key={org + user} client={client} />
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Demo />
  </StrictMode>,
);
