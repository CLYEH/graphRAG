import { Link } from "react-router-dom";

export function NotFound() {
  return (
    <section className="placeholder">
      <h1>Not found</h1>
      <p>
        No page here. <Link to="/">Go to the console home.</Link>
      </p>
    </section>
  );
}
