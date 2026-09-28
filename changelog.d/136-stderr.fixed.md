- The exposure warning now reaches a piped stderr immediately. Before Python
  3.9 a stderr that is not a terminal is block-buffered, and the mock then
  serves forever without filling the buffer, so under `docker logs` or any
  supervisor the warning that the control plane is unauthenticated arrived
  only when the server stopped. Both standard streams are line-buffered at
  startup now, not just stdout.
