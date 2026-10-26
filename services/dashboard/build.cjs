   const esbuild = require("esbuild");
   const fs = require("fs");

   fs.mkdirSync("dist", { recursive: true });
   esbuild.buildSync({
     entryPoints: ["src/main.jsx"],
     bundle: true,
     minify: true,
     target: "es2020",
     define: { "process.env.NODE_ENV": '"production"' },
     outfile: "dist/app.js",
     nodePaths: (process.env.NODE_PATH || "").split(require("path").delimiter).filter(Boolean),
   });
   for (const f of ["index.html", "styles.css"]) fs.copyFileSync(`src/${f}`, `dist/${f}`);
   console.log("dashboard built -> dist/");