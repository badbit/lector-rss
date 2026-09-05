# rssmovil

Cliente Android del lector RSS, en Flutter. Lee sin conexión de una copia SQLite
propia y sincroniza contra el hub con el mismo diario de cambios que el
escritorio: reloj de Lamport, última escritura gana **por campo**, cola de salida
y replicación parcial por ámbito.

No usa servicios de Google Play, para poder empaquetarse en F-Droid.

La guía completa —cadena de compilación en `$HOME`, pruebas sin dispositivo,
contrato de la API y los puntos que hay que respetar al tocar la sincronización—
está en [`../docs/android.md`](../docs/android.md).

```bash
flutter test      # 13 pruebas del motor de sincronización, sin emulador
flutter analyze
flutter build apk --debug
```

## Licencia

AGPL-3.0-or-later, como el resto del repositorio. El texto completo está en
[`../LICENSE`](../LICENSE).
