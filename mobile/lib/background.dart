/// Sincronización periódica mediante Android WorkManager.
library;

import 'package:flutter/widgets.dart';
import 'package:workmanager/workmanager.dart';

import 'app_state.dart';

const backgroundSyncTask = 'org.rsscore.rssmovil.sync';

@pragma('vm:entry-point')
void callbackDispatcher() {
  Workmanager().executeTask((task, inputData) async {
    WidgetsFlutterBinding.ensureInitialized();
    if (task != backgroundSyncTask) return true;
    try {
      final estado = await AppState.crear();
      if (!estado.configurado) return true;
      await estado.sincronizar();
      return estado.ultimoError == null;
    } catch (_) {
      // WorkManager reintentará con su política de backoff.
      return false;
    }
  });
}

Future<void> configurarSincronizacionEnSegundoPlano(bool enabled) async {
  await Workmanager().initialize(callbackDispatcher);
  if (!enabled) {
    await Workmanager().cancelByUniqueName(backgroundSyncTask);
    return;
  }
  await Workmanager().registerPeriodicTask(
    backgroundSyncTask,
    backgroundSyncTask,
    frequency: const Duration(minutes: 30),
    constraints: Constraints(
      networkType: NetworkType.connected,
      requiresBatteryNotLow: true,
      requiresStorageNotLow: true,
    ),
  );
}
