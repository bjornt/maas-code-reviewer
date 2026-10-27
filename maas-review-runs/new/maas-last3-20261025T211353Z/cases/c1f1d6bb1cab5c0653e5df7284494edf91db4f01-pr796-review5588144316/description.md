Avoid repeated mocking on every test. Mocks are created once in the fixture using the service class as the spec. Correct the type hint for the services_mock to be `Mock` instead of `ServiceCollectionV3`.

Also improve the `AsyncOpenFGAClientMock` to make it possible to pass pool permissions.