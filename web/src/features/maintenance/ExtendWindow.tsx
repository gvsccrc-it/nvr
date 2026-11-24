import { useState } from "react";
import { failureText } from "@/features/admin/tabDraft";
import { useExtendWindow, type MaintenanceWindow } from "./api";

const STEPS: [number, string][] = [[30, "30 minutes"], [60, "1 hour"]];

/** Moving a window's end later, for whoever may end it: offered on every running window, and the answer to its "ends soon" reminder. */
export function ExtendWindow({ window }: { window: MaintenanceWindow }) {
  const extend = useExtendWindow();
  const [error, setError] = useState<string | null>(null);
  if (!window.can_end || window.state === "ended") return null;
  return (
    <span className="extend-window">
      {window.ending_soon ? <span className="tag" data-tone="warn">Ends soon</span> : null}
      {STEPS.map(([minutes, label]) => (
        <button key={minutes} type="button" className="button" disabled={extend.isPending}
                onClick={() => { setError(null); extend.mutate({ id: window.id, minutes }, { onError: problem => setError(failureText(problem, "The window could not be extended.")) }); }}>
          {`Extend ${window.name} by ${label}`}
        </button>
      ))}
      {error ? <span className="muted" role="alert">{error}</span> : null}
    </span>
  );
}
