// Each v1 area is built in its own later task; FE0 ships the shell around them.
export function Placeholder({ title, task }: { title: string; task: string }) {
  return (
    <section className="placeholder">
      <h1>{title}</h1>
      <p>
        This area lands in {task}. FE0 wires the app shell, typed API client, and project switcher.
      </p>
    </section>
  );
}
