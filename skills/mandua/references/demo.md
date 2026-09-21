# Preparar o localizar la demo de Mandu’a

Leé esta guía únicamente cuando el usuario pida preparar o explorar la demo.
Es un caso ficticio para probar la skill; no es la memoria predeterminada para
otras consultas. `SKILL_DIR` sigue siendo la carpeta de la skill canónica.

## Consultar una demo existente

Tomá la carpeta de prueba indicada por el usuario; si dice «este directorio», usá
el directorio de trabajo actual. Dentro de esa carpeta, la memoria está en
`mandua-demo/repository`, no en la carpeta de prueba, `clone` ni `remote.git`.
Usá esa ruta absoluta con el auxiliar pertinente según el SKILL.md.

Si falta o es inválida, informalo y pedí la ruta correcta o una solicitud de
preparación. Una pregunta sobre la demo no autoriza crearla o recrearla.

## Preparar la demo a pedido

Para una solicitud explícita de preparación, reemplazá `TRIAL_DIR` por la carpeta
de prueba elegida y `SKILL_DIR` por la ubicación de la skill. Ejecutá una sola vez:

```bash
if [ -f "TRIAL_DIR/mandua-demo/report.json" ] && [ -e "TRIAL_DIR/mandua-demo/repository/.git" ]; then
  printf 'Demo existente: %s\n' "TRIAL_DIR/mandua-demo/repository"
elif [ -e "TRIAL_DIR/mandua-demo" ]; then
  printf 'La carpeta mandua-demo existe pero no parece una demo completa. No se modificó.\n' >&2
  exit 1
else
  "SKILL_DIR/scripts/mandua" demo --output "TRIAL_DIR/mandua-demo" --format human
fi
```

Después de una preparación exitosa, entregá la ruta absoluta de
`TRIAL_DIR/mandua-demo/repository` como la memoria seleccionada para esta
conversación. Conservá esa ruta para las preguntas siguientes. Si reutilizaste una
demo, la primera consulta confirmará el acceso a su historial.

No leas `report.json` para responder: es un reporte de pruebas, no el índice de
memoria. No crees otra demo para cada pregunta ni inicialices Git en su carpeta
padre. Para conocer el contexto ficticio, el usuario puede leer
[la historia del huerto](../../../docs/demo-story.md).
