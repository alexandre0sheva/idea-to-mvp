import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import App from "./App";

describe("App", () => {
  it("renders the heading", () => {
    render(<App />);
    expect(screen.getByRole("heading")).toBeInTheDocument();
  });

  it("counts clicks", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveTextContent("Clicked 1 times");
  });
});
