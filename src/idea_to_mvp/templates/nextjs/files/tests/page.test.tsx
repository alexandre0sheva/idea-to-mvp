import { render, screen } from "@testing-library/react";

import Home from "../app/page";

describe("Home", () => {
  it("renders the heading", () => {
    render(<Home />);
    expect(screen.getByRole("heading")).toBeInTheDocument();
  });
});
