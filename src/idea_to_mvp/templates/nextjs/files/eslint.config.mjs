import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const config = [
  { ignores: [".next/**", "out/**", "coverage/**", "node_modules/**", ".idea-to-mvp/**", "next-env.d.ts"] },
  ...nextVitals,
  ...nextTypescript,
];

export default config;
