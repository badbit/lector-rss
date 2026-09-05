/// Lector de un artículo.
///
/// Se renderiza el HTML sin motor web completo: ni JavaScript ni peticiones
/// remotas, igual que en el cliente de escritorio. Abrir un artículo no avisa a
/// nadie de que lo has leído.
library;

import 'package:flutter/material.dart';
import 'package:flutter_widget_from_html_core/flutter_widget_from_html_core.dart';
import 'package:intl/intl.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app_state.dart';
import '../models.dart';

class ArticuloPage extends StatefulWidget {
  const ArticuloPage({
    super.key,
    required this.estado,
    required this.entrada,
    required this.feedTitulo,
  });

  final AppState estado;
  final Entry entrada;
  final String feedTitulo;

  @override
  State<ArticuloPage> createState() => _ArticuloPageState();
}

class _ArticuloPageState extends State<ArticuloPage> {
  late Entry _entrada = widget.entrada;
  bool _cargando = true;

  @override
  void initState() {
    super.initState();
    _cargar();
  }

  Future<void> _cargar() async {
    // Al abrirlo se marca como leído: es lo que espera cualquiera que venga de
    // un lector de feeds.
    if (!_entrada.read) {
      await widget.estado.marcarLeido([_entrada.id], true);
      _entrada = _entrada.copyWith(read: true);
    }
    final completo = await widget.estado.cargarArticulo(_entrada.id);
    if (!mounted) return;
    setState(() {
      if (completo != null) _entrada = completo;
      _cargando = false;
    });
  }

  Future<void> _alternarGuardado() async {
    final nuevo = !_entrada.starred;
    await widget.estado.marcarGuardado([_entrada.id], nuevo);
    if (!mounted) return;
    setState(() => _entrada = _entrada.copyWith(starred: nuevo));
  }

  Future<void> _exportar(String destino) async {
    try {
      if (destino == 'obsidian') {
        await widget.estado.exportarObsidian(_entrada.id);
      } else {
        await widget.estado.enviarKindle(_entrada.id);
      }
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(destino == 'obsidian'
                ? 'Exportación a Obsidian encolada'
                : 'Artículo enviado al Kindle'),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$e')));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final tema = Theme.of(context);
    final fecha =
        DateFormat('d MMM yyyy, HH:mm', 'es').format(_entrada.published);

    return Scaffold(
      appBar: AppBar(
        title: Text(widget.feedTitulo, overflow: TextOverflow.ellipsis),
        actions: [
          if (_entrada.url?.isNotEmpty ?? false)
            IconButton(
              tooltip: 'Abrir el original',
              icon: const Icon(Icons.open_in_browser),
              onPressed: () => launchUrl(
                Uri.parse(_entrada.url!),
                mode: LaunchMode.externalApplication,
              ),
            ),
          IconButton(
            tooltip: _entrada.starred ? 'Quitar de guardados' : 'Guardar',
            icon: Icon(_entrada.starred ? Icons.star : Icons.star_border),
            color: _entrada.starred ? Colors.amber : null,
            onPressed: _alternarGuardado,
          ),
          PopupMenuButton<String>(
            tooltip: 'Exportar',
            onSelected: _exportar,
            itemBuilder: (_) => const [
              PopupMenuItem(
                  value: 'obsidian', child: Text('Enviar a Obsidian')),
              PopupMenuItem(value: 'kindle', child: Text('Enviar al Kindle')),
            ],
          ),
          IconButton(
            tooltip: 'Marcar como no leído',
            icon: const Icon(Icons.mark_email_unread_outlined),
            onPressed: () async {
              await widget.estado.marcarLeido([_entrada.id], false);
              if (context.mounted) Navigator.pop(context);
            },
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 48),
        children: [
          Text(_entrada.title, style: tema.textTheme.headlineSmall),
          const SizedBox(height: 8),
          Text(
            [widget.feedTitulo, _entrada.author, fecha]
                .where((e) => e != null && e.isNotEmpty)
                .join(' · '),
            style: tema.textTheme.bodySmall?.copyWith(color: tema.hintColor),
          ),
          const Divider(height: 32),
          if (_cargando && !_entrada.hasBody)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 32),
              child: Center(child: CircularProgressIndicator()),
            )
          else
            _cuerpo(tema),
        ],
      ),
    );
  }

  Widget _cuerpo(ThemeData tema) {
    if (_entrada.bodyHtml?.isNotEmpty ?? false) {
      return HtmlWidget(
        _entrada.bodyHtml!,
        textStyle: tema.textTheme.bodyLarge?.copyWith(height: 1.55),
        // Sin conexión el cuerpo puede traer imágenes que no cargan; se deja
        // que fallen en silencio en vez de romper la lectura.
        onErrorBuilder: (_, e, __) => Text('[no se pudo mostrar: $e]'),
        onLoadingBuilder: (_, __, ___) => const SizedBox(height: 8),
      );
    }
    final texto = _entrada.bodyText ?? _entrada.summary;
    if (texto == null || texto.isEmpty) {
      return Text(
        'Este artículo no tiene contenido descargado.\n\n'
        'Ábrelo en el navegador o sincroniza con el hub estando conectado.',
        style: tema.textTheme.bodyMedium?.copyWith(color: tema.hintColor),
      );
    }
    return Text(texto, style: tema.textTheme.bodyLarge?.copyWith(height: 1.55));
  }
}
