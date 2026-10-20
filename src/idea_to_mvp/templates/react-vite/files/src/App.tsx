import { useState } from "react";

export default function App() {
  const [count, setCount] = useState(0);

  return (
    <main>
      <h1>{{name}}</h1>
      <button onClick={() => setCount((value) => value + 1)}>Clicked {count} times</button>
    </main>
  );
}
